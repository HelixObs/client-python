# tests

Unit tests for the helixobs Python client. No real gateway or network required — all tests use `InMemorySpanExporter`.

## Fixtures (conftest.py)

- `exporter` — a fresh `InMemorySpanExporter` per test.
- `instrument` — an `Instrument` instance wired to `exporter` via `SimpleSpanProcessor` (synchronous export, no background thread delay).
- `finished_spans(exporter)` — helper returning all finished spans as a list.

The `instrument` fixture bypasses `Instrument.__init__` (which calls `trace.set_tracer_provider` globally) and wires a private provider directly to the instance, preventing test-to-test provider leakage.

## Test files

| File | Covers |
|---|---|
| `test_store.py` | TraceStore: put/get, maxsize eviction, thread safety |
| `test_instrument.py` | All three layers: track/complete/error, stage context manager, stage decorator, parent resolution, N-to-1 provenance |
| `test_logging.py` | configure_logging() factory: field injection inside/outside spans, idempotency |
| `test_chime.py` | CHIMEInstrument: instrument ID, all metadata helpers, end-to-end provenance chain |

## Adding tests for a new domain

Follow the `test_chime.py` pattern:
1. Create a domain-specific `@pytest.fixture` that builds a `<Domain>Instrument` with `InMemorySpanExporter`.
2. Test each metadata helper method.
3. Add an end-to-end test covering the full provenance chain for that domain.
