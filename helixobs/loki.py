"""
helixobs.loki
─────────────
Non-blocking Grafana Loki log handler.

Records are enqueued and shipped in a background daemon thread so that
pipeline threads are never blocked by network I/O.  If the queue fills
(default 10 000 records) new records are silently dropped — preferable to
stalling a telescope pipeline.

Uses only Python stdlib (urllib, queue, threading, json).

Usage:
    from helixobs.logging import configure_logging, configure_loki

    configure_logging()          # inject helix context fields into every record
    configure_loki(
        url="http://loki:3100",
        labels={"app": "chime", "env": "prod"},
    )
"""

import json
import logging
import queue
import threading
import time
import urllib.request

_FLUSH_INTERVAL = 1.0   # seconds between forced flushes
_BATCH_SIZE     = 100   # flush immediately when batch reaches this size
_QUEUE_MAX      = 10_000


class LokiHandler(logging.Handler):
    """Async logging handler that pushes batched records to Grafana Loki."""

    def __init__(
        self,
        url: str,
        labels: dict[str, str] | None = None,
        timeout: float = 5.0,
        level: int = logging.NOTSET,
    ) -> None:
        """
        Args:
            url:     Loki base URL, e.g. ``http://loki:3100``.  The push
                     path ``/loki/api/v1/push`` is appended automatically.
            labels:  Static stream labels added to every log line.
            timeout: HTTP request timeout in seconds.
            level:   Minimum log level forwarded to Loki.
        """
        super().__init__(level)
        self._url = url.rstrip("/") + "/loki/api/v1/push"
        self._static_labels: dict[str, str] = dict(labels or {})
        self._timeout = timeout
        self._queue: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="helixobs-loki"
        )
        self._thread.start()

    # ── logging.Handler interface ─────────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """Enqueue *record* for async delivery; drops silently when full."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            pass

    def close(self) -> None:
        """Flush remaining records and stop the background thread."""
        self._queue.put(None)           # sentinel — worker exits after draining
        self._thread.join(timeout=5.0)
        super().close()

    # ── background worker ─────────────────────────────────────────────────────

    def _worker(self) -> None:
        batch: list[logging.LogRecord] = []
        deadline = time.monotonic() + _FLUSH_INTERVAL

        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                if batch:
                    self._push(batch)
                    batch = []
                deadline = time.monotonic() + _FLUSH_INTERVAL
                continue

            if item is None:            # sentinel
                if batch:
                    self._push(batch)
                return

            batch.append(item)
            if len(batch) >= _BATCH_SIZE:
                self._push(batch)
                batch = []
                deadline = time.monotonic() + _FLUSH_INTERVAL

    def _push(self, records: list[logging.LogRecord]) -> None:
        """Serialize *records* and POST to the Loki push endpoint."""
        # Group by label-set so each unique combination forms its own stream.
        streams: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        for record in records:
            labels = dict(self._static_labels)
            entity_id     = getattr(record, "helixEntityID",     "")
            instrument_id = getattr(record, "helixInstrumentID", "")
            if entity_id:
                labels["helix_entity_id"] = entity_id
            if instrument_id:
                labels["helix_instrument_id"] = instrument_id

            key    = tuple(sorted(labels.items()))
            ts_ns  = str(int(record.created * 1_000_000_000))
            line   = self.format(record)
            streams.setdefault(key, []).append([ts_ns, line])

        payload = {
            "streams": [
                {"stream": dict(key), "values": values}
                for key, values in streams.items()
            ]
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception:
            pass  # never propagate from the background thread
