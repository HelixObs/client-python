# client-python

Python client library for HelixObs — entity-centric observability for scientific instrument pipelines.

## What this is

A thin OTel wrapper that lets pipeline engineers track domain entities (data blocks, candidates, events) across disjoint async pipeline stages. Entities are correlated via a DAG of `parent_ids`, which the gateway resolves into OTel span links server-side.

The library **never blocks the pipeline** — all OTLP exports happen in the OTel SDK's `BatchSpanProcessor` background thread.

## Package layout

```
helixobs/
  __init__.py          public API: Instrument, Token, CHIMEInstrument
  instrument.py        Instrument class — all three integration layers
  _store.py            TraceStore — in-process entity_id → SpanContext map
  logging.py           configure_logging() — log-record factory
  chime/
    __init__.py        CHIMEInstrument + semantic convention attribute constants
tests/
  conftest.py          shared fixtures (InMemorySpanExporter-backed Instrument)
  test_store.py        TraceStore unit tests
  test_instrument.py   Instrument unit tests (all layers)
  test_logging.py      logging factory tests
  test_chime.py        CHIMEInstrument tests
```

## Key design decisions

- **Three layers**: Layer 0 (`track/complete/error`), Layer 1 (context manager), Layer 2 (decorator). All emit identical OTLP.
- **Parent resolution**: known parents (same process) → OTel `Link`s. Unknown parents (cross-process) → `helix.parent.ids` comma-separated attribute; gateway resolves server-side.
- **Entity operations**: `operate()` starts a new root trace (blank OTel context) for post-creation work on an existing entity. The gateway detects `helix.entity.is_operation = "true"` and writes to `entity_operations` instead of `entities`. The entity's TraceStore entry is never overwritten by an operation.
- **Log factory, not filter**: `configure_logging()` installs a log-record factory (not a root-logger filter) so child loggers that propagate upward are also covered.
- **No custom queue**: `BatchSpanProcessor` already handles async export with local buffering and retry.
- **Loki cardinality**: `helix_entity_id` is NOT a Loki stream label — it would create one stream per entity and exhaust Loki's stream limit. It is embedded in JSON and filtered at query time via `| json | helix_entity_id = "..."`.

## Span attribute names (gateway contract)

| Attribute | Value |
|---|---|
| `helix.entity.id` | entity ID |
| `helix.instrument.id` | instrument identifier (e.g. "CHIME") |
| `helix.parent.ids` | comma-separated unresolved parent IDs (cross-process fallback) |
| `helix.entity.is_operation` | `"true"` when span is an operation on an existing entity, not a new entity |

## Gateway event contract

Any span event whose name starts with `helix.` is extracted by the gateway and written to the `entity_events` TimescaleDB table. This applies to both entity creation spans and operation spans.

- `helix.error` — entity or operation failed (set by `instrument.error()` or `op.fail()`)
- `helix.event.<name>` — scientifically notable signal (set by `token.add_event()` or `op.add_event()`)

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

## Adding a new instrument domain

1. Create `helixobs/<domain>/__init__.py` subclassing `Instrument`.
2. Define attribute key constants matching the semantic conventions doc.
3. Add metadata helper methods (analogous to `CHIMEInstrument.candidate_metadata`).
4. Add tests in `tests/test_<domain>.py` using the shared `conftest.py` fixture pattern.
