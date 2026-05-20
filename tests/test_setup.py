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


class TestSetupAuthForwarding:
    """setup() must forward credential and auth_endpoint iff the instrument_class accepts them."""

    def _auth_instrument_class(self, received: dict):
        """Returns an Instrument subclass that records kwargs and accepts auth params."""
        class AuthInstrument(Instrument):
            def __init__(
                self,
                service_name,
                *,
                instrument_id,
                endpoint="localhost:4317",
                insecure=True,
                credential=None,
                auth_endpoint=None,
            ):
                received.update(
                    instrument_id=instrument_id,
                    credential=credential,
                    auth_endpoint=auth_endpoint,
                )
                self.instrument_id = instrument_id
        return AuthInstrument

    def test_credential_string_forwarded(self):
        received: dict = {}
        cls = self._auth_instrument_class(received)
        setup(
            "svc.test",
            instrument_id="INST",
            credential="my-secret",
            auth_endpoint="https://gw/auth/token",
            instrument_class=cls,
        )
        assert received["credential"] == "my-secret"

    def test_credential_callable_forwarded(self):
        received: dict = {}
        cls = self._auth_instrument_class(received)
        getter = lambda: "dynamic-token"
        setup(
            "svc.test",
            instrument_id="INST",
            credential=getter,
            auth_endpoint="https://gw/auth/token",
            instrument_class=cls,
        )
        assert received["credential"] is getter

    def test_auth_endpoint_forwarded(self):
        received: dict = {}
        cls = self._auth_instrument_class(received)
        setup(
            "svc.test",
            instrument_id="INST",
            credential="secret",
            auth_endpoint="https://gw/auth/token",
            instrument_class=cls,
        )
        assert received["auth_endpoint"] == "https://gw/auth/token"

    def test_credential_none_not_forwarded(self):
        """credential=None (the default) must not be passed to the constructor."""
        received: dict = {}
        cls = self._auth_instrument_class(received)
        setup("svc.test", instrument_id="INST", instrument_class=cls)
        # credential defaults to None and is not explicitly forwarded
        assert received.get("credential") is None

    def test_credential_not_forwarded_to_subclass_without_param(self):
        """credential must not be forwarded to constructors that don't accept it."""
        received: dict = {}

        class NoAuthInstrument(Instrument):
            def __init__(self, service_name, *, instrument_id, endpoint, insecure):
                received["kwargs"] = dict(
                    instrument_id=instrument_id, endpoint=endpoint, insecure=insecure
                )
                self.instrument_id = instrument_id

        setup(
            "svc.test",
            instrument_id="INST",
            credential="secret",
            auth_endpoint="https://gw/auth/token",
            instrument_class=NoAuthInstrument,
        )
        assert "credential" not in received["kwargs"]
        assert "auth_endpoint" not in received["kwargs"]

    def test_process_name_forwarded_when_in_signature(self):
        received: dict = {}

        class ProcInstrument(Instrument):
            def __init__(
                self,
                service_name,
                *,
                instrument_id,
                endpoint,
                insecure,
                process_name=None,
            ):
                received["process_name"] = process_name
                self.instrument_id = instrument_id

        setup(
            "svc.test",
            instrument_id="INST",
            process_name="node-07",
            instrument_class=ProcInstrument,
        )
        assert received["process_name"] == "node-07"


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
