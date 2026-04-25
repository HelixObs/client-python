"""Tests for helixobs.logging — log-record factory context injection."""

import logging
import os

import pytest

import helixobs.logging as helix_logging


@pytest.fixture(autouse=True)
def reset_logging_factory(monkeypatch):
    """Restore the original log-record factory after each test."""
    original = logging.getLogRecordFactory()
    helix_logging._installed = False
    # Ensure env vars used by the factory are absent unless a test sets them.
    monkeypatch.delenv("GITHUB_REPO",    raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
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
        assert record.otel_trace_id == ""
        assert record.otel_span_id == ""

    def test_entity_id_empty_outside_span(self):
        helix_logging.configure_logging()
        record = _make_record()
        assert record.helix_entity_id == ""
        assert record.helix_instrument_id == ""


class TestFactoryInsideSpan:
    def test_trace_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        record = _make_record()
        instrument.complete(token)
        assert record.otel_trace_id != ""
        assert len(record.otel_trace_id) == 32  # 128-bit trace ID as hex

    def test_span_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        record = _make_record()
        instrument.complete(token)
        assert record.otel_span_id != ""
        assert len(record.otel_span_id) == 16  # 64-bit span ID as hex

    def test_entity_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="my-entity")
        record = _make_record()
        instrument.complete(token)
        assert record.helix_entity_id == "my-entity"

    def test_ids_empty_after_span_ends(self, instrument):
        helix_logging.configure_logging()
        token = instrument.track("stage", id="entity-1")
        instrument.complete(token)
        record = _make_record()
        assert record.otel_trace_id == ""

    def test_context_manager_injects_during_block(self, instrument):
        helix_logging.configure_logging()
        records: list[logging.LogRecord] = []
        with instrument.stage("stage", id="block-1"):
            records.append(_make_record("inside"))
        records.append(_make_record("outside"))
        assert records[0].otel_trace_id != ""
        assert records[1].otel_trace_id == ""


class TestSourceLocation:
    def test_src_tag_appended_to_message(self):
        helix_logging.configure_logging()
        record = _make_record("hello")
        assert "  src=" in record.msg

    def test_helix_source_attribute_set(self):
        helix_logging.configure_logging()
        record = _make_record("hello")
        assert hasattr(record, "helix_source")
        assert record.helix_source != ""

    def test_src_contains_lineno(self):
        helix_logging.configure_logging()
        record = _make_record()
        # record.lineno is 1 as set by _make_record
        assert "#L1" in record.helix_source

    def test_src_fallback_no_env_vars(self):
        helix_logging.configure_logging()
        record = _make_record()
        # Without env vars, should be a plain path, not a URL
        assert "github.com" not in record.helix_source

    def test_src_github_permalink_when_env_vars_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPO",    "https://github.com/HelixObs/client-python")
        monkeypatch.setenv("GIT_COMMIT_SHA", "abc123def456")
        helix_logging.configure_logging()
        record = _make_record()
        assert record.helix_source.startswith(
            "https://github.com/HelixObs/client-python/blob/abc123def456/"
        )
        assert "#L" in record.helix_source

    def test_src_trailing_slash_stripped_from_repo_url(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPO",    "https://github.com/HelixObs/client-python/")
        monkeypatch.setenv("GIT_COMMIT_SHA", "sha1")
        helix_logging.configure_logging()
        record = _make_record()
        assert "//blob" not in record.helix_source

    def test_record_args_cleared(self):
        helix_logging.configure_logging()
        record = _make_record("value=%s")
        # args were () to begin with; factory must not break when args is empty
        assert record.args is None

    def test_message_preserved_in_msg(self):
        helix_logging.configure_logging()
        record = _make_record("pipeline started")
        assert record.msg.startswith("pipeline started")


class TestNormalizePath:
    def test_site_packages_stripped(self):
        path = "/usr/lib/python3.13/site-packages/helixobs/instrument.py"
        result = helix_logging._normalize_path(path, "instrument.py")
        assert result == "helixobs/instrument.py"

    def test_relative_path_returned_for_local_file(self):
        # Use a file that actually exists so relpath works deterministically.
        abs_path = os.path.abspath(__file__)
        result = helix_logging._normalize_path(abs_path, os.path.basename(abs_path))
        assert not os.path.isabs(result)

    def test_basename_fallback(self):
        # Simulate a ValueError from os.path.relpath (Windows cross-drive).
        import unittest.mock as mock
        with mock.patch("os.path.relpath", side_effect=ValueError):
            result = helix_logging._normalize_path("/some/abs/path/file.py", "file.py")
        assert result == "file.py"
