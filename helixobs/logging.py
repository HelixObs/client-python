"""
helixobs.logging
────────────────
Log context injection and optional Loki shipping.

``configure_logging()`` installs a log-record factory that injects helix
context fields (otelTraceID, otelSpanID, helixEntityID, helixInstrumentID)
into every log record emitted anywhere in the process, including from
sub-modules and third-party libraries.

``configure_loki()`` attaches a non-blocking handler that ships records to
Grafana Loki.  Call *after* ``configure_logging()`` so that helix fields are
already present on the records.

Usage (once at application startup):

    from helixobs.logging import configure_logging, configure_loki

    configure_logging()
    configure_loki(url="http://loki:3100", labels={"app": "chime"})
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from helixobs.loki import LokiHandler


def configure_logging() -> None:
    """Install the helix log-record factory on the root logger.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    _install_factory()


def configure_loki(
    url: str,
    labels: dict[str, str] | None = None,
    level: int = logging.NOTSET,
    timeout: float = 5.0,
    logger: logging.Logger | None = None,
) -> "LokiHandler":
    """Attach a non-blocking Loki handler to *logger* (default: root logger).

    Args:
        url:     Loki base URL, e.g. ``http://loki:3100``.
        labels:  Static stream labels added to every log line.
        level:   Minimum log level forwarded to Loki (default: all levels).
        timeout: HTTP request timeout for each push, in seconds.
        logger:  Logger to attach to; defaults to the root logger.

    Returns:
        The :class:`~helixobs.loki.LokiHandler` instance, so callers can
        ``handler.close()`` during a clean shutdown or change the formatter.
    """
    from helixobs.loki import LokiHandler

    handler = LokiHandler(url=url, labels=labels, timeout=timeout, level=level)
    target = logger if logger is not None else logging.getLogger()
    target.addHandler(handler)
    return handler


# ── Implementation ────────────────────────────────────────────────────────────

_installed = False


def _install_factory() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    _prev = logging.getLogRecordFactory()

    def _factory(name, level, fn, lno, msg, args, exc_info,
                 func=None, sinfo=None):
        record = _prev(name, level, fn, lno, msg, args, exc_info, func, sinfo)
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc.is_valid:
            record.otelTraceID   = format(sc.trace_id, "032x")
            record.otelSpanID    = format(sc.span_id,  "016x")
            attrs = getattr(span, "attributes", {}) or {}
            record.helixEntityID     = attrs.get("helix.entity.id", "")
            record.helixInstrumentID = attrs.get("helix.instrument.id", "")
        else:
            record.otelTraceID       = ""
            record.otelSpanID        = ""
            record.helixEntityID     = ""
            record.helixInstrumentID = ""
        return record

    logging.setLogRecordFactory(_factory)
