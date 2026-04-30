# helixobs — Python client

Entity-centric observability for scientific instrument pipelines.

Track domain entities (data blocks, beam candidates, astrophysical events) across disjoint asynchronous pipeline stages. The library attaches provenance links between entities, so you can trace any scientific result all the way back to the raw sensor data that produced it.

Built on [OpenTelemetry](https://opentelemetry.io/). Signals are standard OTLP — the HelixObs gateway adds entity intelligence on top.

## Installation

```bash
pip install helixobs
```

## Quick start

```python
from helixobs import Instrument
from helixobs.logging import configure_logging

# Once at startup
configure_logging()
tel = Instrument(
    service_name="chime.frb-classifier",
    instrument_id="CHIME",
    endpoint="interceptor.local:4317",
)

# Track a pipeline entity (context manager — recommended)
with tel.create("frb_classifier", id=cand_id, parents=[block_id]) as token:
    result = classify(candidate)
    if result.confidence > 0.95:
        token.add_event("helix.event.candidate_promoted", {
            "snr": result.snr,
            "dm":  result.dm,
        })
# token ends here — exported asynchronously, pipeline never blocked
```

## Three integration layers

All three layers emit identical OTLP signals. Choose the one that fits your code structure.

### Layer 0 — Primitive

For long-running daemons, event loops, or any structure where a context manager is awkward.

```python
token = tel.create("x-engine", id="block-1234", parents=[]).start()
result = process_data_block(block)
token.complete()

# On failure:
token.error(metadata={"message": "BUFFER_OVERFLOW", "rack": "gpu-rack-3"})
```

### Layer 1 — Context manager (recommended)

`complete()` is called automatically on normal exit. `error()` is called automatically on any unhandled exception.

```python
with tel.create("frb_classifier", id=cand_id, parents=[block_id]) as token:
    result = classify(candidate)

    if result.rfi_probability > 0.9:
        token.add_event("helix.event.rfi_flagged", {
            "fraction_flagged": result.rfi_probability,
            "algorithm":        "spectral_kurtosis",
        })
```

### Layer 2 — Decorator

For functions that map cleanly to a single pipeline stage. Use callables for `id` and `parents` when they depend on the function's arguments.

```python
@tel.create(
    "frb_classifier",
    id=lambda cand: cand.id,
    parents=lambda cand: [cand.block_id],
)
def classify_candidate(cand):
    return run_classifier(cand)
```

## Internal sub-steps with `child_span()`

Use `child_span()` for work that belongs inside one entity's trace — RFI mitigation, dedispersion, per-beam clustering — without registering a new entity. Sub-steps appear in Tempo as nested spans.

```python
token = tel.create("l1-search", id=beam_id, parents=[block_id]).start()

with tel.child_span("rfi-mitigation") as span:
    n = excise_rfi(data)
    span.set_attribute("helix.chime.rfi_flagged_channels", n)
    log.info(f"RFI mitigation: {n} channels excised")

with tel.child_span("dedispersion") as span:
    dm_trials = run_dedispersion(data)
    span.set_attribute("helix.chime.dm_trials", dm_trials)

token.complete()
```

## Post-creation operations with `operate()`

Some work happens on an entity after its creation trace has ended — often asynchronously in separate processes. These are not new entities; they are **operations on an existing entity**, each with their own independent OTel trace. The Entity Inspector shows all traces for an entity across its lifetime.

```python
# After the FRB event entity is created and its detection trace is closed:

with tel.operate("hdf5-conversion", entity_id=event_id) as op:
    size_mb = write_hdf5(event_id)
    op.set_attribute("helix.chime.hdf5_size_mb", size_mb)
    if failed:
        log.error("HDF5 write failed: disk full")
        op.error({"message": "hdf5_write_error"})
        return

with tel.operate("registration", entity_id=event_id) as op:
    register_in_catalog(event_id)
    log.info("event registered")

for dest in ["cedar", "niagara"]:
    with tel.operate("replication", entity_id=event_id) as op:
        op.set_attribute("helix.chime.replication_dest", dest)
        replicate_to(dest)
        log.info(f"replicated to {dest}")
```

If the entity doesn't exist when an operation arrives (race condition or late arrival), the gateway creates a minimal placeholder automatically.

## N-to-1 provenance

The defining feature of HelixObs: a single entity can have multiple parents from independent processing chains. Standard OTel tracing can't model this.

```python
# Three beam candidates, processed independently
for beam_id in [41, 42, 43]:
    with tel.create("beam-processor", id=f"cand-beam{beam_id}", parents=[block_id]):
        process_beam(beam_id)

# One astrophysical event formed from all three
with tel.create("clustering", id="frb-20260415-042",
                parents=["cand-beam41", "cand-beam42", "cand-beam43"]) as token:
    event = cluster_candidates(candidates)
```

The gateway resolves all three parent IDs into OTel span links, so Tempo shows the full DAG.

## Log context injection

One call at startup injects `trace_id`, `span_id`, `entity_id`, and `instrument_id` into every log record emitted anywhere in the process — including from third-party libraries — while a span is active.

```python
from helixobs.logging import configure_logging
configure_logging()

import logging
log = logging.getLogger("chime.pipeline")

with tel.create("frb_classifier", id=cand_id, parents=[block_id]):
    log.info("classifying candidate")
    # Record now carries: otel_trace_id, otel_span_id, helix_entity_id, helix_instrument_id
```

These fields are indexed by Loki and enable the Grafana cross-signal navigation path:
metric anomaly → Loki log line → Tempo trace → parent entity trace.

## Span events

Use `token.add_event()` to attach named events to an entity's span:

```python
with tel.create("classifier", id=cand_id) as token:
    token.add_event("helix.event.rfi_flagged",        {"fraction_flagged": 0.92})
    token.add_event("helix.event.candidate_promoted",  {"snr": 23.4, "confidence": 0.97})
    token.add_event("helix.event.voevent_issued",      {"ivorn": "ivo://chime/frb/2026a"})
    token.add_event("helix.event.archived",            {"path": "/data/chime/2026/frb042"})
```

The gateway extracts every `helix.*` event and writes it to TimescaleDB using the event's own timestamp, producing a complete queryable timeline for each entity.

## Non-blocking guarantee

`create()`, `operate()`, `token.complete()`, `token.error()`, and the context manager enter/exit **never perform network I/O on the calling thread**. All OTLP exports happen in the OTel SDK's `BatchSpanProcessor` background thread. If the gateway is unreachable, spans queue locally and are retried silently. The instrument pipeline is never aware of any observability infrastructure failure.

## Development

```bash
pip install -e ".[dev]"
pytest
```
