# helixobs package

Core library code. Entry point is `Instrument` in `instrument.py`.

## Files

- **instrument.py** — `Instrument`, `Token`. All pipeline-facing API lives here.
- **_store.py** — `TraceStore`: thread-safe bounded dict mapping `entity_id → SpanContext`. Used to resolve in-process parents into OTel `Link`s. Operations do NOT overwrite entity entries here.
- **logging.py** — `configure_logging(*, otlp=False)`: installs a log-record factory that injects `otelTraceID`, `otelSpanID`, `helixEntityID`, `helixInstrumentID` into every log record while a span is active. Default mode writes JSON to stdout (sidecar collection). `otlp=True` ships logs via `OTLPLogExporter` over gRPC to the OTel Collector — no sidecar required.
- **chime/** — `CHIMEInstrument` subclass + CHIME semantic convention attribute constants.

## Attribute contract with the herald

The herald extracts these attributes from every incoming span:

```
helix.entity.id          string   entity ID (required — triggers HelixObs processing)
helix.instrument.id      string   instrument identifier
helix.parent.ids         string   comma-separated unresolved parent IDs (optional)
helix.entity.is_operation string  "true" → write entity_operations row, not entities row
```

Do not rename these without updating the herald interceptor constants (`attrEntityID`, etc.).

## Entity vs Operation vs Plain Trace

| | `create()` | `operate()` | `trace()` |
|---|---|---|---|
| Creates entity row | Yes | No (upserts placeholder if missing) | No |
| Creates operation row | No | Yes | No |
| Overwrites TraceStore | Yes | No | No |
| OTel trace | New root (or linked) | New root (blank context) | Inherits current context |
| Herald processing | entity write | entity_operations write | passthrough |
| Use when | Entity comes into being | Work done on an existing entity | Infrastructure work with no entity |

## Deferred entity_id on operate()

`entity_id` is optional on `operate()`. When omitted, the span starts immediately (covering
all pre-discovery work) and `helix.entity.id` is set later once the entity is known:

```python
with tel.operate("stage-replication", site=SITE) as operation:
    file_replicas = await fetch_replicas(...)   # logged under this trace
    if not file_replicas:
        return []                               # span closes with no entity_id → passthrough
    dataset_name = await resolve_dataset(...)
    operation.set_entity_id(dataset_name)       # now linked to the entity
    deposit(work)
```

If the span closes without `entity_id` ever being set, a `WARNING` is logged and the span
is forwarded by the herald as a plain OTel trace (no `entity_operations` row written). This
is intentional for early-exit paths (queue full, nothing to process).

`set_attribute("helix.entity.id", value)` is equivalent to `set_entity_id(value)` and
also suppresses the warning.

## tel.trace() — plain OTel spans

For infrastructure work that has no entity (HTTP handlers, background loops, retry daemons),
use `tel.trace()` instead of importing the raw OTel tracer:

```python
with tel.trace("handle-request", attributes={"method": "POST"}) as span:
    with tel.child_span("validate-payload"):
        ...
    with tel.child_span("query-db"):
        ...
```

All child spans created via `tel.child_span()` inside a `tel.trace()` block automatically
inherit the trace context — they share the same `otelTraceID` and appear nested in Tempo.
The herald forwards `tel.trace()` spans unchanged (no `helix.entity.id` → passthrough).
Logs emitted inside get `otelTraceID` and `otelSpanID` injected by the log factory, making
them filterable by trace ID in Loki.

## Adding new integration layers or helpers

- New layers go in `instrument.py` — keep `Instrument` as the single entry point.
- New domain helpers go in `helixobs/<domain>/__init__.py` as `Instrument` subclasses.
- All new public symbols must be exported from `__init__.py`.
