# helixobs package

Core library code. Entry point is `Instrument` in `instrument.py`.

## Files

- **instrument.py** — `Instrument`, `Token`, `_StageHelper`. All pipeline-facing API lives here.
- **_store.py** — `TraceStore`: thread-safe bounded dict mapping `entity_id → SpanContext`. Used to resolve in-process parents into OTel `Link`s.
- **logging.py** — `configure_logging()`: installs a log-record factory that injects `otelTraceID`, `otelSpanID`, `helixEntityID`, `helixInstrumentID` into every log record while a span is active.
- **chime/** — `CHIMEInstrument` subclass + CHIME semantic convention attribute constants.

## Attribute contract with the gateway

The gateway extracts these three attributes from every incoming span:

```
helix.entity.id       string   entity ID
helix.instrument.id   string   instrument identifier
helix.parent.ids      string   comma-separated unresolved parent IDs (optional)
```

Do not rename these without updating the gateway interceptor.

## Adding new integration layers or helpers

- New layers go in `instrument.py` — keep `Instrument` as the single entry point.
- New domain helpers go in `helixobs/<domain>/__init__.py` as `Instrument` subclasses.
- All new public symbols must be exported from `__init__.py`.
