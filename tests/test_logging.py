"""Tests for helixobs.logging — log-record factory context injection."""

import json
import logging
import os
import sys

import pytest

import helixobs.logging as helix_logging


@pytest.fixture(autouse=True)
def reset_logging_factory(monkeypatch):
    """Restore the original log-record factory and handler guards after each test."""
    original = logging.getLogRecordFactory()
    helix_logging._installed = False
    helix_logging._json_handler_installed = False
    helix_logging._otlp_handler_installed = False
    helix_logging._log_provider = None
    # Ensure env vars used by the factory are absent unless a test sets them.
    monkeypatch.delenv("GITHUB_REPO",    raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    yield
    logging.setLogRecordFactory(original)
    helix_logging._installed = False
    helix_logging._json_handler_installed = False
    helix_logging._otlp_handler_installed = False
    helix_logging._log_provider = None


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
        token = instrument.create("stage", id="entity-1").start()
        record = _make_record()
        token.complete()
        assert record.otel_trace_id != ""
        assert len(record.otel_trace_id) == 32  # 128-bit trace ID as hex

    def test_span_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.create("stage", id="entity-1").start()
        record = _make_record()
        token.complete()
        assert record.otel_span_id != ""
        assert len(record.otel_span_id) == 16  # 64-bit span ID as hex

    def test_entity_id_injected_inside_span(self, instrument):
        helix_logging.configure_logging()
        token = instrument.create("stage", id="my-entity").start()
        record = _make_record()
        token.complete()
        assert record.helix_entity_id == "my-entity"

    def test_ids_empty_after_span_ends(self, instrument):
        helix_logging.configure_logging()
        token = instrument.create("stage", id="entity-1").start()
        token.complete()
        record = _make_record()
        assert record.otel_trace_id == ""

    def test_context_manager_injects_during_block(self, instrument):
        helix_logging.configure_logging()
        records: list[logging.LogRecord] = []
        with instrument.create("stage", id="block-1"):
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

    def test_src_no_github_permalink_for_site_packages(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPO",    "https://github.com/HelixObs/client-python")
        monkeypatch.setenv("GIT_COMMIT_SHA", "abc123")
        helix_logging.configure_logging()
        record = logging.getLogger("test").makeRecord(
            "test", logging.INFO,
            "/usr/lib/python3.13/site-packages/numpy/core/fromnumeric.py",
            42, "msg", (), None,
        )
        assert "github.com" not in record.helix_source
        assert "numpy/core/fromnumeric.py#L42" in record.helix_source

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


class TestOtlpRequiresServiceName:
    def test_raises_without_service_name(self):
        with pytest.raises(ValueError, match="service_name"):
            helix_logging.configure_logging(otlp=True)

    def test_raises_with_empty_service_name(self):
        with pytest.raises(ValueError, match="service_name"):
            helix_logging.configure_logging(otlp=True, service_name="")

    def test_no_error_without_otlp(self):
        # service_name is ignored in stdout mode — no error
        helix_logging.configure_logging(otlp=False, service_name=None)


class TestUpdateLogServiceName:
    def test_noop_when_otlp_not_configured(self):
        # _log_provider is None — must not raise
        helix_logging.update_log_service_name("my-service")

    def test_noop_with_empty_string(self):
        helix_logging.update_log_service_name("")


# ── install_context_fields ────────────────────────────────────────────────────

class TestInstallContextFields:
    def test_installs_factory(self):
        helix_logging.install_context_fields()
        assert helix_logging._installed is True

    def test_idempotent(self):
        helix_logging.install_context_fields()
        factory1 = logging.getLogRecordFactory()
        helix_logging.install_context_fields()
        factory2 = logging.getLogRecordFactory()
        assert factory1 is factory2


# ── _HelixJsonFormatter exc_info branch ──────────────────────────────────────

class TestJsonFormatterExcInfo:
    def test_exc_info_included_in_output(self):
        formatter = helix_logging._HelixJsonFormatter()
        try:
            raise ValueError("test error from formatter")
        except ValueError:
            exc = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="f.py", lineno=1,
            msg="something failed", args=(), exc_info=exc,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exc" in data
        assert "ValueError" in data["exc"]


# ── _install_json_handler else-branch (root has no handlers) ─────────────────

class TestInstallJsonHandlerNoHandlers:
    def test_adds_stream_handler_when_root_has_no_handlers(self):
        root = logging.getLogger()
        saved = root.handlers[:]
        root.handlers = []
        try:
            helix_logging._install_json_handler()
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0].formatter, helix_logging._HelixJsonFormatter)
        finally:
            root.handlers = saved
            helix_logging._json_handler_installed = False


# ── update_log_service_name with process_name ─────────────────────────────────

class TestUpdateLogServiceNameProcessName:
    def test_sets_process_name_global(self, monkeypatch):
        monkeypatch.setattr(helix_logging, "_process_name", "")
        helix_logging.update_log_service_name("svc", process_name="beam-proc")
        assert helix_logging._process_name == "beam-proc"

    def test_process_name_unchanged_when_not_given(self, monkeypatch):
        monkeypatch.setattr(helix_logging, "_process_name", "existing")
        helix_logging.update_log_service_name("svc")
        assert helix_logging._process_name == "existing"


# ── update_log_service_name with log_provider ─────────────────────────────────

class TestUpdateLogServiceNameWithProvider:
    def test_updates_provider_resource(self, monkeypatch):
        from unittest import mock
        mock_provider = mock.MagicMock()
        monkeypatch.setattr(helix_logging, "_log_provider", mock_provider)
        helix_logging.update_log_service_name("new-service")
        assert mock_provider._resource is not None

    def test_updates_provider_resource_with_process_name(self, monkeypatch):
        from unittest import mock
        mock_provider = mock.MagicMock()
        monkeypatch.setattr(helix_logging, "_log_provider", mock_provider)
        monkeypatch.setattr(helix_logging, "_process_name", "")
        helix_logging.update_log_service_name("new-service", process_name="p1")
        assert helix_logging._process_name == "p1"
        assert mock_provider._resource is not None

    def test_exception_in_resource_update_is_swallowed(self, monkeypatch):
        from unittest import mock
        from opentelemetry.sdk.resources import Resource
        mock_provider = mock.MagicMock()
        monkeypatch.setattr(helix_logging, "_log_provider", mock_provider)
        monkeypatch.setattr(Resource, "create", mock.MagicMock(side_effect=RuntimeError("boom")))
        helix_logging.update_log_service_name("svc")  # must not raise


# ── _install_otlp_handler ─────────────────────────────────────────────────────

class TestInstallOtlpHandler:
    def test_configure_logging_otlp_succeeds(self, monkeypatch):
        pytest.importorskip(
            "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
            reason="opentelemetry-exporter-otlp-proto-grpc not installed",
        )
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            helix_logging.configure_logging(otlp=True, service_name="test-svc")
            assert helix_logging._otlp_handler_installed is True
            assert helix_logging._log_provider is not None
        finally:
            root.handlers = saved

    def test_configure_logging_otlp_idempotent(self, monkeypatch):
        pytest.importorskip(
            "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
            reason="opentelemetry-exporter-otlp-proto-grpc not installed",
        )
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            helix_logging.configure_logging(otlp=True, service_name="test-svc")
            provider_first = helix_logging._log_provider
            # second call — guard keeps the same provider
            helix_logging.configure_logging(otlp=True, service_name="other-svc")
            assert helix_logging._log_provider is provider_first
        finally:
            root.handlers = saved
