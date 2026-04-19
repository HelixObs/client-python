"""
helixobs.instrument
───────────────────
Instrument — the single entry point for pipeline engineers.

Three integration layers (all emit identical OTLP signals):

  Layer 0 — Primitive (for daemons, long-running loops, or any structure
            where a context manager is impractical):

      token = tel.track('frb_classifier', id=cand_id, parents=[block_id])
      result = classify(candidate)
      tel.complete(token)                          # or tel.error(token, ...)

  Layer 1 — Context manager (recommended for structured Python code):

      with tel.stage('frb_classifier', id=cand_id, parents=[block_id]) as span:
          result = classify(candidate)
          span.add_event('helix.event.candidate_promoted', {'snr': result.snr})
          # complete() called automatically; error() called on unhandled exception

  Layer 2 — Decorator (when one function maps cleanly to one pipeline stage):

      @tel.stage('frb_classifier',
                 id=lambda cand: cand.id,
                 parents=lambda cand: [cand.block_id])
      def classify_candidate(cand):
          return run_classifier(cand)

Non-blocking guarantee
──────────────────────
track() / complete() / error() / __enter__ / __exit__ never perform network
I/O on the calling thread.  All OTLP exports happen in the BatchSpanProcessor
background thread maintained by the OTel SDK.  If the gateway is unreachable,
spans queue locally and are retried silently.

Parent resolution
─────────────────
When parents=[block_id], the library looks up each ID in the in-process
TraceStore and adds an OTel span link.  For parents created in a different
process (not in the local store), the IDs are set as the helix.parent.ids
attribute; the gateway resolves them server-side.
"""

import functools
import logging
from contextlib import contextmanager
from typing import Callable, Union

_log = logging.getLogger("helixobs.instrument")

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, NonRecordingSpan, StatusCode

from ._store import TraceStore

# Canonical span attribute names — must match what the gateway extracts.
_ATTR_ENTITY_ID     = "helix.entity.id"
_ATTR_INSTRUMENT_ID = "helix.instrument.id"
_ATTR_PARENT_IDS    = "helix.parent.ids"    # comma-separated; cross-process fallback
_ATTR_IS_OPERATION  = "helix.entity.is_operation"


# ── Token ─────────────────────────────────────────────────────────────────────

class Token:
    """
    Returned by Instrument.track().  Passed to complete() or error().
    Also exposed as the context-manager variable (the `as span` target).
    """

    def __init__(self, span, ctx_token, entity_id: str) -> None:
        self._span = span
        self._ctx_token = ctx_token
        self._entity_id = entity_id
        self._done = False

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        """Attach a timestamped event to this entity's span.

        Use helix.error for errors on a known entity.
        Use helix.event.<name> for scientifically notable signals.
        """
        self._span.add_event(name, attributes=attributes or {})


# ── _StageHelper ──────────────────────────────────────────────────────────────

class _StageHelper:
    """
    Returned by Instrument.stage().  Supports both context-manager and
    decorator usage — the caller decides which pattern to use.
    """

    def __init__(
        self,
        instrument: "Instrument",
        stage_name: str,
        entity_id: Union[str, Callable],
        parents: Union[list[str], Callable],
    ) -> None:
        self._instrument = instrument
        self._stage_name = stage_name
        self._entity_id = entity_id
        self._parents = parents
        self._token: Token | None = None

    # ── Context manager ───────────────────────────────────────────────

    def __enter__(self) -> Token:
        eid = self._entity_id() if callable(self._entity_id) else self._entity_id
        pids = self._parents() if callable(self._parents) else self._parents
        self._token = self._instrument.track(self._stage_name, id=eid, parents=pids)
        return self._token

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self._token is not None
        if exc_type is not None:
            self._instrument.error(self._token, metadata={"exception.message": str(exc_val)})
        else:
            self._instrument.complete(self._token)
        return False  # never suppress exceptions

    # ── Decorator ─────────────────────────────────────────────────────

    def __call__(self, fn: Callable) -> Callable:
        """When used as @tel.stage(...), wraps fn as a single pipeline stage."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            eid = self._entity_id(*args, **kwargs) if callable(self._entity_id) else self._entity_id
            pids = self._parents(*args, **kwargs) if callable(self._parents) else self._parents
            token = self._instrument.track(self._stage_name, id=eid, parents=pids)
            try:
                result = fn(*args, **kwargs)
                self._instrument.complete(token)
                return result
            except Exception as exc:
                self._instrument.error(token, metadata={"exception.message": str(exc)})
                raise
        return wrapper


# ── _OperationHandle ──────────────────────────────────────────────────────────

class _OperationHandle:
    """
    Yielded by Instrument.operate().  Wraps the underlying OTel span so callers
    don't need to import opentelemetry directly.
    """

    def __init__(self, span) -> None:
        self._span = span

    def set_attribute(self, key: str, value) -> None:
        self._span.set_attribute(key, str(value))

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self._span.add_event(name, attributes=attributes or {})

    def fail(self, message: str = "operation failed") -> None:
        """Mark this operation as failed."""
        self._span.set_status(StatusCode.ERROR)
        self._span.add_event("helix.error", attributes={"message": message})


# ── Instrument ────────────────────────────────────────────────────────────────

class Instrument:
    """
    Entry point for a single instrument's pipeline observability.

    Instantiate once at service startup; share across pipeline stages.

    Parameters
    ----------
    service_name:
        OTel service name — identifies this pipeline stage in Tempo/Grafana.
        e.g. "chime.frb-classifier"
    instrument_id:
        Instrument identifier stamped on every entity span.
        e.g. "CHIME"
    endpoint:
        OTLP gRPC endpoint of the HelixObs gateway (or OTel Collector).
        e.g. "interceptor.local:4317"
    insecure:
        Use plaintext gRPC.  True for on-prem deployments without TLS.

    Example
    -------
    from helixobs import Instrument

    tel = Instrument("chime.frb-classifier", instrument_id="CHIME",
                     endpoint="interceptor.local:4317")

    # Layer 1 — context manager
    with tel.stage('frb_classifier', id=cand_id, parents=[block_id]) as span:
        result = classify(candidate)
        if result.confidence > 0.95:
            span.add_event('helix.event.candidate_promoted',
                           {'snr': result.snr, 'dm': result.dm})
    """

    def __init__(
        self,
        service_name: str,
        instrument_id: str,
        endpoint: str = "localhost:4317",
        insecure: bool = True,
    ) -> None:
        self.instrument_id = instrument_id
        self._store = TraceStore()

        resource = Resource.create({
            "service.name": service_name,
            _ATTR_INSTRUMENT_ID: instrument_id,
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
            )
        )
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("helixobs", tracer_provider=provider)
        self._provider = provider

    # ── Internal span lifecycle ────────────────────────────────────────

    def _resolve_links(self, parent_ids: list[str]) -> tuple[list[Link], list[str]]:
        links, unresolved = [], []
        for pid in parent_ids:
            ctx = self._store.get(pid)
            if ctx is not None:
                links.append(Link(ctx))
            else:
                unresolved.append(pid)
        return links, unresolved

    # ── Layer 0 — Primitive API ────────────────────────────────────────

    def track(
        self,
        stage_name: str,
        *,
        id: str,
        parents: list[str] | None = None,
    ) -> Token:
        """Start tracking an entity.  Returns a Token passed to complete() or error()."""
        links, unresolved = self._resolve_links(parents or [])

        span = self._tracer.start_span(stage_name, links=links)
        span.set_attribute(_ATTR_ENTITY_ID, id)
        span.set_attribute(_ATTR_INSTRUMENT_ID, self.instrument_id)
        # Always include all parent entity IDs so the gateway can persist the
        # full provenance DAG in TimescaleDB, regardless of whether parents
        # were resolved to span links in-process or not.
        if parents:
            span.set_attribute(_ATTR_PARENT_IDS, ",".join(parents))

        ctx = trace.set_span_in_context(span)
        ctx_token = context_api.attach(ctx)

        self._store.put(id, span.get_span_context())
        return Token(span, ctx_token, id)

    def complete(self, token: Token, metadata: dict | None = None) -> None:
        """Mark an entity as successfully processed."""
        if token._done:
            return
        token._done = True
        if metadata:
            for k, v in metadata.items():
                token._span.set_attribute(k, str(v))
        context_api.detach(token._ctx_token)
        token._span.end()

    def error(self, token: Token, metadata: dict | None = None) -> None:
        """Record a helix.error event and mark the entity span as failed."""
        if token._done:
            return
        token._done = True
        attrs = {k: str(v) for k, v in (metadata or {}).items()}
        token._span.add_event("helix.error", attributes=attrs)
        token._span.set_status(StatusCode.ERROR)
        # Emit a structured log while the span context is still attached so
        # the factory injects helix_entity_id and otel_trace_id automatically.
        _log.error(attrs.get("message", "entity error"))
        context_api.detach(token._ctx_token)
        token._span.end()

    # ── Child spans (internal pipeline steps, no entity registration) ──

    @contextmanager
    def child_span(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        attributes: dict | None = None,
    ):
        """
        Create a child span within an entity's trace without registering a
        new HelixObs entity.  Use for internal pipeline steps that should
        appear in Tempo's span tree but don't need provenance tracking.

        If parent_id is given, the span is parented to that entity's stored
        SpanContext (works even after the entity token has been completed).
        If omitted, the span is parented to the current active context.
        """
        if parent_id is not None:
            span_ctx = self._store.get(parent_id)
            ctx = (
                trace.set_span_in_context(NonRecordingSpan(span_ctx))
                if span_ctx is not None
                else context_api.get_current()
            )
        else:
            ctx = context_api.get_current()
        with self._tracer.start_as_current_span(name, context=ctx) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, v)
            yield span

    # ── Entity operations (new independent trace on an existing entity) ──

    @contextmanager
    def operate(self, operation: str, *, entity_id: str):
        """
        Start an independent trace for an operation performed on an existing
        entity.  The entity must already exist.  Records an entity_operation
        row in the DB (not a new entity row) so the Entity Inspector can show
        all traces associated with one entity across its lifetime.

        Yields an _OperationHandle for attribute-setting and failure recording.
        Unhandled exceptions automatically mark the operation as failed.

        Example::

            with tel.operate("hdf5-conversion", entity_id=event_id) as op:
                op.set_attribute("helix.chime.hdf5_size_mb", size_mb)
                if failure:
                    op.fail("hdf5_write_error")
                    return None
        """
        with self._tracer.start_as_current_span(
            operation,
            context=context_api.Context(),  # blank context → new root trace
        ) as span:
            span.set_attribute(_ATTR_ENTITY_ID, entity_id)
            span.set_attribute(_ATTR_INSTRUMENT_ID, self.instrument_id)
            span.set_attribute(_ATTR_IS_OPERATION, "true")
            handle = _OperationHandle(span)
            try:
                yield handle
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR)
                raise

    # ── Layer 1 + 2 — stage() ─────────────────────────────────────────

    def stage(
        self,
        stage_name: str,
        *,
        id: Union[str, Callable],
        parents: Union[list[str], Callable, None] = None,
    ) -> _StageHelper:
        """Return a _StageHelper usable as a context manager or decorator."""
        return _StageHelper(self, stage_name, id, parents or [])

    # ── Lifecycle ─────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Flush pending spans and shut down the exporter.  Call at process exit."""
        self._provider.shutdown()
