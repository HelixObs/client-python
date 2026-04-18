"""Tests for helixobs.loki.LokiHandler and helixobs.logging.configure_loki."""

import json
import logging
import time
import unittest.mock as mock

import pytest

import helixobs.logging as helix_logging
from helixobs.loki import LokiHandler


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_handler(url: str = "http://loki:3100", **kw) -> LokiHandler:
    return LokiHandler(url=url, **kw)


def _wait_idle(handler: LokiHandler, timeout: float = 2.0) -> None:
    """Wait until the handler's internal queue drains."""
    deadline = time.monotonic() + timeout
    while not handler._queue.empty():
        if time.monotonic() > deadline:
            raise TimeoutError("LokiHandler queue did not drain in time")
        time.sleep(0.01)
    time.sleep(0.05)   # give the worker one extra tick to finish the push


def _capture_push(handler: LokiHandler) -> list[dict]:
    """Patch urlopen on *handler* and return all pushed payloads."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data))
        return mock.MagicMock().__enter__.return_value

    with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
        yield captured


# ── LokiHandler unit tests ────────────────────────────────────────────────────

class TestLokiHandlerURL:
    def test_push_path_appended(self):
        h = _make_handler(url="http://loki:3100")
        assert h._url == "http://loki:3100/loki/api/v1/push"
        h.close()

    def test_trailing_slash_stripped(self):
        h = _make_handler(url="http://loki:3100/")
        assert h._url == "http://loki:3100/loki/api/v1/push"
        h.close()


class TestLokiHandlerEmit:
    def test_payload_contains_stream(self):
        h = _make_handler(labels={"app": "test"})
        payloads: list[dict] = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data))
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.MagicMock(return_value=None)
            ctx.__exit__ = mock.MagicMock(return_value=False)
            return ctx

        with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
            record = logging.LogRecord("t", logging.INFO, "f.py", 1, "hello", (), None)
            h.emit(record)
            h.close()

        assert len(payloads) >= 1
        streams = payloads[0]["streams"]
        assert len(streams) == 1
        assert streams[0]["stream"]["app"] == "test"
        ts, line = streams[0]["values"][0]
        assert line == "hello"
        assert int(ts) > 0

    def test_helix_labels_added_when_present(self):
        h = _make_handler()
        payloads: list[dict] = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data))
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.MagicMock(return_value=None)
            ctx.__exit__ = mock.MagicMock(return_value=False)
            return ctx

        with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
            record = logging.LogRecord("t", logging.INFO, "f.py", 1, "msg", (), None)
            record.helixEntityID = "entity-42"
            record.helixInstrumentID = "CHIME"
            h.emit(record)
            h.close()

        stream_labels = payloads[0]["streams"][0]["stream"]
        assert stream_labels["helix_entity_id"] == "entity-42"
        assert stream_labels["helix_instrument_id"] == "CHIME"

    def test_helix_labels_absent_when_empty(self):
        h = _make_handler()
        payloads: list[dict] = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data))
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.MagicMock(return_value=None)
            ctx.__exit__ = mock.MagicMock(return_value=False)
            return ctx

        with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
            record = logging.LogRecord("t", logging.INFO, "f.py", 1, "msg", (), None)
            record.helixEntityID = ""
            record.helixInstrumentID = ""
            h.emit(record)
            h.close()

        stream_labels = payloads[0]["streams"][0]["stream"]
        assert "helix_entity_id" not in stream_labels
        assert "helix_instrument_id" not in stream_labels

    def test_records_grouped_by_label_set(self):
        h = _make_handler()
        payloads: list[dict] = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data))
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.MagicMock(return_value=None)
            ctx.__exit__ = mock.MagicMock(return_value=False)
            return ctx

        with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
            r1 = logging.LogRecord("t", logging.INFO, "f.py", 1, "r1", (), None)
            r1.helixEntityID = "e1"
            r1.helixInstrumentID = "CHIME"

            r2 = logging.LogRecord("t", logging.INFO, "f.py", 1, "r2", (), None)
            r2.helixEntityID = "e2"
            r2.helixInstrumentID = "CHIME"

            r3 = logging.LogRecord("t", logging.INFO, "f.py", 1, "r3", (), None)
            r3.helixEntityID = "e1"
            r3.helixInstrumentID = "CHIME"

            h.emit(r1)
            h.emit(r2)
            h.emit(r3)
            h.close()

        streams = payloads[0]["streams"]
        # r1 and r3 share the same entity → same stream; r2 is separate
        assert len(streams) == 2

    def test_full_queue_does_not_block(self):
        h = _make_handler()
        h._queue.maxsize = 1
        # Fill the queue so the second put_nowait would raise queue.Full
        h._queue.put_nowait(
            logging.LogRecord("t", logging.INFO, "f.py", 1, "fill", (), None)
        )
        # This must not raise or block
        record = logging.LogRecord("t", logging.INFO, "f.py", 1, "drop", (), None)
        h.emit(record)   # should silently drop
        h.close()

    def test_urlopen_error_does_not_propagate(self):
        h = _make_handler()
        with mock.patch(
            "helixobs.loki.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", (), None)
            h.emit(record)
            h.close()   # must not raise


class TestLokiHandlerLevel:
    def test_level_set_on_handler(self):
        # The Logger skips handlers whose level is too high (record.levelno < handler.level).
        # Verify the handler exposes the configured level so Logger filtering works.
        h = _make_handler(level=logging.WARNING)
        assert h.level == logging.WARNING
        h.close()

    def test_level_accepted_above_threshold(self):
        h = _make_handler(level=logging.WARNING)
        payloads: list[dict] = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data))
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.MagicMock(return_value=None)
            ctx.__exit__ = mock.MagicMock(return_value=False)
            return ctx

        with mock.patch("helixobs.loki.urllib.request.urlopen", side_effect=fake_urlopen):
            warn_r = logging.LogRecord("t", logging.WARNING, "f.py", 1, "warn", (), None)
            # Logger checks record.levelno >= handler.level before calling emit()
            if warn_r.levelno >= h.level:
                h.emit(warn_r)
            h.close()

        all_lines = [
            v[1]
            for p in payloads
            for s in p["streams"]
            for v in s["values"]
        ]
        assert "warn" in all_lines


# ── configure_loki integration ────────────────────────────────────────────────

class TestConfigureLoki:
    @pytest.fixture(autouse=True)
    def reset_logging(self):
        original_factory = logging.getLogRecordFactory()
        helix_logging._installed = False
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        yield
        logging.setLogRecordFactory(original_factory)
        helix_logging._installed = False
        for h in list(root.handlers):
            if h not in handlers_before:
                root.removeHandler(h)
                h.close()

    def test_handler_attached_to_root_by_default(self):
        from helixobs.logging import configure_loki
        h = configure_loki(url="http://loki:3100")
        assert h in logging.getLogger().handlers
        h.close()

    def test_handler_attached_to_named_logger(self):
        from helixobs.logging import configure_loki
        named = logging.getLogger("test.loki.named")
        h = configure_loki(url="http://loki:3100", logger=named)
        assert h in named.handlers
        h.close()

    def test_returns_loki_handler_instance(self):
        from helixobs.logging import configure_loki
        h = configure_loki(url="http://loki:3100")
        assert isinstance(h, LokiHandler)
        h.close()
