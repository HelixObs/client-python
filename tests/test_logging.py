"""Tests for helixobs.logging — log-record factory context injection."""

import logging

import pytest

# Re-import resets the _installed guard for each test module load, but since
# the factory is global we need to reset it between tests.
import helixobs.logging as helix_logging


@pytest.fixture(autouse=True)
def reset_logging_factory():
    """Restore the original log-record factory after each test."""
    original = logging.getLogRecordFactory()
    helix_logging._installed = False
    yield
    logging.setLogRecordFactory(original)
    helix_logging._installed = False


def _make_record(msg: str = "test") -> logging.LogRecord:
    logger = logging.getLogger("test.helixobs")
    return logger.makeRecord("test", logging.INFO, "f.py", 1, msg, (), None)


class TestConfigureLogging:
    def test_idempotent_double_call(self):
        helix_logging.configure_logging()
        factory_after_first = logging.getLogRecordFactory()
        helix_logging.configure_logging()
        factory_after_second = logging.getLogRecordFactory()
        assert factory_after_first is factory_after_second

    def test_installed_flag_set(self):
        helix_logging.configure_logging()
        assert helix_logging._installed is True


class TestFactoryOutsideSpan:
    def test_trace_id_empty_outside_span(self):
        helix_logging.configure_logging()
        record = _make_record()
        assert record.otelTraceID == ""
        assert record.otelSpanID == ""

    def test_entity_id_empty_outside_span(self):
        helix_logging.configure_logging()
        record = _make_record()
        assert record.helixEntityID == ""
        assert record.helixInstrumentID == ""


class TestFactoryInsideSpan:
    def test_trace_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        record = _make_record()
        instrument.complete(token)
        assert record.otelTraceID != ""
        assert len(record.otelTraceID) == 32  # 128-bit trace ID as hex

    def test_span_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        record = _make_record()
        instrument.complete(token)
        assert record.otelSpanID != ""
        assert len(record.otelSpanID) == 16  # 64-bit span ID as hex

    def test_entity_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="my-entity")
        record = _make_record()
        instrument.complete(token)
        assert record.helixEntityID == "my-entity"

    def test_ids_empty_after_span_ends(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        instrument.complete(token)
        record = _make_record()
        assert record.otelTraceID == ""

    def test_context_manager_injects_during_block(self, instrument):
        helix_logging.configure_logging()
        records: list[logging.LogRecord] = []
        with instrument.stage("stage", id="block-1"):
            records.append(_make_record("inside"))
        records.append(_make_record("outside"))
        assert records[0].otelTraceID != ""
        assert records[1].otelTraceID == ""
