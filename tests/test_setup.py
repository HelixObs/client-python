"""Tests for helixobs.setup — setup() convenience entry point."""

import os
import unittest.mock as mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import helixobs.logging as helix_logging
from helixobs.instrument import Instrument
from helixobs.setup import setup


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch):
    helix_logging._installed = False
    helix_logging._json_handler_installed = False
    helix_logging._otlp_handler_installed = False
    helix_logging._log_provider = None
    yield
    helix_logging._installed = False
    helix_logging._json_handler_installed = False
    helix_logging._otlp_handler_installed = False
    helix_logging._log_provider = None


@pytest.fixture
def patched_instrument(monkeypatch):
    """Patch Instrument.__init__ to avoid registering a global TracerProvider."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _fake_init(self, service_name, *, instrument_id, endpoint, insecure):
        self.instrument_id = instrument_id
        from helixobs._store import TraceStore
        from opentelemetry import trace
        self._store = TraceStore()
        self._tracer = trace.get_tracer("helixobs", tracer_provider=provider)
        self._provider = provider

    monkeypatch.setattr(Instrument, "__init__", _fake_init)
    return exporter


class TestSetupReturnsInstrument:
    def test_returns_instrument_by_default(self, patched_instrument):
        tel = setup("svc.test", instrument_id="TEST")
        assert isinstance(tel, Instrument)

    def test_instrument_id_set(self, patched_instrument):
        tel = setup("svc.test", instrument_id="MY_INST")
        assert tel.instrument_id == "MY_INST"

    def test_raises_when_base_class_and_no_instrument_id(self, patched_instrument):
        with pytest.raises(ValueError, match="instrument_id"):
            setup("svc.test")

    def test_subclass_without_instrument_id_does_not_raise(self):
        """A subclass that owns its own instrument_id needs no instrument_id from caller."""
        class SelfNamingInstrument(Instrument):
            def __init__(self, service_name, *, endpoint="localhost:4317", insecure=True):
                # doesn't call super() — just records what it was given
                self.instrument_id = "SELF"
                self.service_name = service_name

        tel = setup("svc.test", instrument_class=SelfNamingInstrument)
        assert isinstance(tel, SelfNamingInstrument)
        assert tel.instrument_id == "SELF"

    def test_subclass_instrument_id_not_passed_when_not_in_signature(self):
        """instrument_id must not be forwarded to constructors that don't accept it."""
        received: dict = {}

        class DomainInstrument(Instrument):
            def __init__(self, service_name, *, endpoint="localhost:4317", insecure=True):
                received["kwargs"] = dict(service_name=service_name,
                                          endpoint=endpoint, insecure=insecure)
                self.instrument_id = "DOMAIN"

        setup("svc.test", instrument_id="IGNORED", instrument_class=DomainInstrument)
        assert "instrument_id" not in received["kwargs"]

    def test_returns_subclass_type(self):
        class MyInstrument(Instrument):
            def __init__(self, service_name, *, instrument_id, endpoint, insecure):
                self.instrument_id = instrument_id

        tel = setup("svc.test", instrument_id="TEST", instrument_class=MyInstrument)
        assert type(tel) is MyInstrument


class TestSetupLogging:
    def test_otlp_false_installs_json_logging(self, patched_instrument):
        setup("svc.test", instrument_id="TEST", otlp=False)
        assert helix_logging._json_handler_installed is True
        assert helix_logging._otlp_handler_installed is False

    def test_factory_installed_after_setup(self, patched_instrument):
        setup("svc.test", instrument_id="TEST", otlp=False)
        assert helix_logging._installed is True


class TestSetupLogEndpoint:
    def test_log_endpoint_sets_env_var(self, patched_instrument, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        with mock.patch.object(helix_logging, "_install_otlp_handler"):
            setup("svc.test", instrument_id="TEST", otlp=True, log_endpoint="http://host:4319")
        assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://host:4319"

    def test_log_endpoint_not_overrides_existing_env(self, patched_instrument, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://existing:4317")
        with mock.patch.object(helix_logging, "_install_otlp_handler"):
            setup("svc.test", instrument_id="TEST", otlp=True, log_endpoint="http://new:4319")
        assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://existing:4317"
