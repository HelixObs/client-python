# helixobs package

Core library code. Entry point is `Instrument` in `instrument.py`.

## Files

- **instrument.py** — `Instrument`, `Token`. All pipeline-facing API lives here.
- **_store.py** — `TraceStore`: thread-safe bounded dict mapping `entity_id → SpanContext`. Used to resolve in-process parents into OTel `Link`s. Operations do NOT overwrite entity entries here.
- **logging.py** — `configure_logging(*, otlp=False)`: installs a log-record factory that injects `otelTraceID`, `otelSpanID`, `helixEntityID`, `helixInstrumentID` into every log record while a span is active. Default mode writes JSON to stdout (sidecar collection). `otlp=True` ships logs via `OTLPLogExporter` over gRPC to the OTel Collector — no sidecar required.
- **chime/** — `CHIMEInstrument` subclass + CHIME semantic convention attribute constants.

## Attribute contract with the gateway

The gateway extracts these attributes from every incoming span:

```
helix.entity.id          string   entity ID (required — triggers HelixObs processing)
helix.instrument.id      string   instrument identifier
helix.parent.ids         string   comma-separated unresolved parent IDs (optional)
helix.entity.is_operation string  "true" → write entity_operations row, not entities row
```

Do not rename these without updating the gateway interceptor constants (`attrEntityID`, etc.).

## Entity vs Operation

| | `create()` | `operate()` |
|---|---|---|
| Creates entity row | Yes | No (upserts placeholder if missing) |
| Creates operation row | No | Yes |
| Overwrites TraceStore | Yes | No |
| OTel trace | New root (or linked) | New root (always blank context) |
| Use when | Entity comes into being | Work done on an existing entity |

## Adding new integration layers or helpers

- New layers go in `instrument.py` — keep `Instrument` as the single entry point.
- New domain helpers go in `helixobs/<domain>/__init__.py` as `Instrument` subclasses.
- All new public symbols must be exported from `__init__.py`.
