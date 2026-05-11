"""
helixobs.instrument
───────────────────
Instrument — the single entry point for pipeline engineers.

Two methods, three integration layers each:

  tel.create(stage, id=..., parents=[...])   — entity comes into existence
  tel.operate(operation, entity_id=...)      — work on an existing entity

Both return a Token usable three ways:

  Layer 0 — Primitive:
      token = tel.create("l1-search", id=beam_id, parents=[block_id])
      token.start()
      token.complete()          # or token.error({"message": "..."})

  Layer 1 — Context manager:
      with tel.create("l1-search", id=beam_id, parents=[block_id]) as token:
          token.add_event("helix.event.candidate_promoted", {"snr": 14.2})

  Layer 2 — Decorator:
      @tel.create("l1-search", id=lambda b: b.id, parents=lambda b: [b.block_id])
      def process_beam(block): ...

Non-blocking guarantee
──────────────────────
start() / complete() / error() never perform network I/O on the calling
thread.  All OTLP exports happen in the BatchSpanProcessor background
thread.  If the gateway is unreachable, spans queue locally and retry.

Parent resolution
─────────────────
Known parents (same process) become OTel span Links.  Unknown parents are
set as helix.parent.ids; the gateway resolves them server-side.
"""

import functools
from typing import Callable, Union

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, NonRecordingSpan, StatusCode

from ._store import TraceStore

_ATTR_ENTITY_ID     = "helix.entity.id"
_ATTR_INSTRUMENT_ID = "helix.instrument.id"
_ATTR_PARENT_IDS    = "helix.parent.ids"
_ATTR_IS_OPERATION  = "helix.entity.is_operation"
_ATTR_LOG_ENTITY_ID = "helix.log.entity_id"


# ── Token ─────────────────────────────────────────────────────────────────────

class Token:
    """
    Returned by Instrument.create() and Instrument.operate().
    Usable as a primitive handle, context manager, or decorator.
    """

    def __init__(
        self,
        instrument: "Instrument",
        stage_name: str,
        entity_id: Union[str, Callable],
        parents: Union[list, Callable],
        is_operation: bool,
    ) -> None:
        self._instrument = instrument
        self._stage_name = stage_name
        self._entity_id = entity_id
        self._parents = parents
        self._is_operation = is_operation
        self._span = None
        self._ctx_token = None
        self._done = False

    # ── Span lifecycle ────────────────────────────────────────────────

    def start(self) -> "Token":
        """Start the underlying span. Returns self for chaining."""
        eid = self._entity_id() if callable(self._entity_id) else self._entity_id
        pids = self._parents() if callable(self._parents) else list(self._parents)
        self._span, self._ctx_token = self._instrument._start_span(
            self._stage_name, eid, pids, self._is_operation
        )
        return self

    def complete(self, metadata: dict | None = None) -> None:
        """End the span successfully."""
        if self._done:
            return
        self._done = True
        if metadata:
            for k, v in metadata.items():
                self._span.set_attribute(k, str(v))
        context_api.detach(self._ctx_token)
        self._span.end()

    def error(self, metadata: dict | None = None) -> None:
        """Record a helix.error event and end the span as failed."""
        if self._done:
            return
        self._done = True
        attrs = {k: str(v) for k, v in (metadata or {}).items()}
        self._span.add_event("helix.error", attributes=attrs)
        self._span.set_status(StatusCode.ERROR)
        context_api.detach(self._ctx_token)
        self._span.end()

    # ── Attribute / event helpers ─────────────────────────────────────

    def set_attribute(self, key: str, value) -> None:
        self._span.set_attribute(key, str(value))

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self._span.add_event(name, attributes=attributes or {})

    # ── Context manager ───────────────────────────────────────────────

    def __enter__(self) -> "Token":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            self.error({"exception.message": str(exc_val)})
        else:
            self.complete()
        return False

    # ── Decorator ─────────────────────────────────────────────────────

    def __call__(self, fn: Callable) -> Callable:
        """When used as @tel.create(...) or @tel.operate(...), wraps fn."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            eid = self._entity_id(*args, **kwargs) if callable(self._entity_id) else self._entity_id
            pids = self._parents(*args, **kwargs) if callable(self._parents) else list(self._parents)
            token = Token(self._instrument, self._stage_name, eid, pids, self._is_operation)
            token.start()
            try:
                result = fn(*args, **kwargs)
                token.complete()
                return result
            except Exception as exc:
                token.error({"exception.message": str(exc)})
                raise
        return wrapper


# ── Instrument ────────────────────────────────────────────────────────────────

class Instrument:
    """
    Entry point for a single instrument's pipeline observability.

    Instantiate once at service startup; share across pipeline stages.

    Parameters
    ----------
    service_name:
        OTel service name — identifies this pipeline in Tempo/Grafana.
    instrument_id:
        Instrument identifier stamped on every entity span.
    endpoint:
        OTLP gRPC endpoint of the HelixObs gateway.
    insecure:
        Use plaintext gRPC. True for on-prem deployments without TLS.
    """

    def __init__(
        self,
        service_name: str,
        instrument_id: str,
        endpoint: str = "localhost:4317",
        insecure: bool = True,
        process_name: str | None = None,
    ) -> None:
        self.instrument_id = instrument_id
        self._store = TraceStore()

        resource_attrs = {
            "service.name": service_name,
            _ATTR_INSTRUMENT_ID: instrument_id,
        }
        if process_name:
            resource_attrs["helix.process.name"] = process_name
        resource = Resource.create(resource_attrs)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
            )
        )
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("helixobs", tracer_provider=provider)
        self._provider = provider

        from .logging import update_log_service_name
        update_log_service_name(service_name, process_name=process_name)

    # ── Internal span factory ─────────────────────────────────────────

    def _resolve_links(self, parent_ids: list[str]) -> tuple[list[Link], list[str]]:
        links, unresolved = [], []
        for pid in parent_ids:
            ctx = self._store.get(pid)
            if ctx is not None:
                links.append(Link(ctx))
            else:
                unresolved.append(pid)
        return links, unresolved

    def _start_span(
        self,
        stage_name: str,
        entity_id: str,
        parent_ids: list[str],
        is_operation: bool,
    ) -> tuple:
        if is_operation:
            ctx = context_api.Context()  # blank → new root trace
            links = []
        else:
            links, _ = self._resolve_links(parent_ids)
            ctx = None

        span = self._tracer.start_span(stage_name, context=ctx, links=links)
        span.set_attribute(_ATTR_ENTITY_ID, entity_id)
        span.set_attribute(_ATTR_INSTRUMENT_ID, self.instrument_id)
        if parent_ids:
            span.set_attribute(_ATTR_PARENT_IDS, ",".join(parent_ids))
        if is_operation:
            span.set_attribute(_ATTR_IS_OPERATION, "true")

        otel_ctx = trace.set_span_in_context(span)
        ctx_token = context_api.attach(otel_ctx)

        if not is_operation:
            self._store.put(entity_id, span.get_span_context())

        return span, ctx_token

    # ── Public API ────────────────────────────────────────────────────

    def create(
        self,
        stage_name: str,
        *,
        id: Union[str, Callable],
        parents: Union[list, Callable, None] = None,
    ) -> Token:
        """Return a Token for a new entity entering the pipeline."""
        return Token(self, stage_name, id, parents or [], is_operation=False)

    def operate(
        self,
        operation: str,
        *,
        entity_id: Union[str, Callable],
    ) -> Token:
        """Return a Token for an operation on an existing entity."""
        return Token(self, operation, entity_id, [], is_operation=True)

    # ── Child spans ───────────────────────────────────────────────────

    def child_span(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        attributes: dict | None = None,
    ):
        """
        Context manager for a child span within an entity's trace without
        registering a new HelixObs entity. Use for internal pipeline steps
        that should appear in Tempo but don't need provenance tracking.
        """
        from contextlib import contextmanager

        @contextmanager
        def _inner():
            if parent_id is not None:
                span_ctx = self._store.get(parent_id)
                ctx = (
                    trace.set_span_in_context(NonRecordingSpan(span_ctx))
                    if span_ctx is not None
                    else context_api.get_current()
                )
            else:
                ctx = context_api.get_current()
            active_attrs = getattr(trace.get_current_span(), "attributes", {}) or {}
            inherited_entity_id = parent_id or (
                active_attrs.get(_ATTR_ENTITY_ID) or active_attrs.get(_ATTR_LOG_ENTITY_ID, "")
            )
            with self._tracer.start_as_current_span(name, context=ctx) as span:
                span.set_attribute(_ATTR_INSTRUMENT_ID, self.instrument_id)
                if inherited_entity_id:
                    span.set_attribute(_ATTR_LOG_ENTITY_ID, inherited_entity_id)
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                yield span

        return _inner()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Flush pending spans and shut down the exporter."""
        self._provider.shutdown()
