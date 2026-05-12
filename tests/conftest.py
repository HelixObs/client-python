"""
Shared fixtures for helixobs tests.

Uses InMemorySpanExporter so tests never need a real gateway.
Each test gets a fresh TracerProvider and Instrument to prevent state leakage.
"""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from helixobs.instrument import Instrument


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def instrument(exporter: InMemorySpanExporter) -> Instrument:
    """A real Instrument wired to an in-memory exporter. No network I/O."""
    tel = Instrument.__new__(Instrument)
    tel.instrument_id = "TEST"

    from helixobs._store import TraceStore
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource

    tel._store = TraceStore()
    tel._process_name = None

    resource = Resource.create({"service.name": "test", "helix.instrument.id": "TEST"})
    provider = TracerProvider(resource=resource)
    # SimpleSpanProcessor exports synchronously — no background thread needed in tests.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tel._tracer = trace.get_tracer("helixobs", tracer_provider=provider)
    tel._provider = provider

    return tel


def finished_spans(exporter: InMemorySpanExporter) -> list:
    return list(exporter.get_finished_spans())
