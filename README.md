# helixobs — Python client

Entity-centric observability for scientific instrument pipelines.

Track domain entities (raw observations, intermediate products, derived results) across disjoint asynchronous pipeline stages. The library attaches provenance links between entities so you can trace any scientific result back to the raw data that produced it.

Built on [OpenTelemetry](https://opentelemetry.io/). Signals are standard OTLP — the HelixObs gateway adds entity intelligence on top.

## Installation

```bash
pip install helixobs
```

Requires Python 3.11+.

## Quick start

```python
from helixobs import setup
import logging

log = logging.getLogger("my.pipeline")

# One call wires both traces and logs with matching service name
tel = setup(
    "my-instrument.pipeline",
    instrument_id="MY_INSTRUMENT",
    endpoint="gateway:4317",
    otlp=True,               # ship logs via OTLP to the collector (port 4319)
)

# Track an entity through a pipeline stage
with tel.create("detector", id=product_id, parents=[frame_id]) as token:
    result = process(item)
    log.info("item processed")           # helix_entity_id injected automatically
    if result.score > 0.95:
        token.add_event("helix.event.detection_confirmed", {"score": result.score})
# complete() called on exit; error() called on unhandled exception
```

**Two OTLP endpoints:**

| Endpoint | Default port | Purpose |
|---|---|---|
| HelixObs Gateway | `4317` | OTLP traces — entity spans |
| OTel Collector | `4319` | OTLP logs — structured log shipping |

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4319` for log shipping, or pass `log_endpoint` to `setup()`.

See [USER_GUIDE.md](USER_GUIDE.md) for the full API reference, integration patterns, structured logging, and data model.

## Development

```bash
pip install -e ".[dev]"
pytest
```
