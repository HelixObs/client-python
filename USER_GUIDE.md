# HelixObs Python Client — User Guide

HelixObs gives scientific instrument pipelines entity-centric observability: every data product (block, candidate, event) gets a persistent identity, a provenance graph, structured logs, and a distributed trace — all queryable from a single Grafana UI.

---

## Table of Contents

1. [Required Infrastructure](#1-required-infrastructure)
2. [Installation](#2-installation)
3. [Quickstart](#3-quickstart)
4. [Core Concepts](#4-core-concepts)
5. [API Reference](#5-api-reference)
6. [Integration Patterns](#6-integration-patterns)
7. [Structured Logging](#7-structured-logging)
8. [Data Model](#8-data-model)
9. [Complete Example](#9-complete-example)

---

## 1. Required Infrastructure

Your pipeline process connects to a single endpoint. The full HelixObs stack runs everything else.

### What your process needs

| Requirement | Details |
|---|---|
| **HelixObs Gateway** | gRPC endpoint (default `localhost:4317`). Every `create()`/`operate()` call sends an OTLP span here. |
| **Log delivery** | Choose one of two modes — see below. |

**Sidecar mode (default):** Write JSON to stdout; Grafana Alloy (or any log collector) tails the container and ships to Loki. Zero extra dependencies.

**OTLP mode (greenfield services):** Call `configure_logging(otlp=True)`. Logs are shipped directly to the OTel Collector over the same gRPC connection used for traces — no sidecar required. The collector's `logs` pipeline forwards them to Loki.

> **In both modes** your pipeline process makes no HTTP calls and no direct Loki calls. All telemetry flows through gRPC.

### HelixObs stack components

The following services must be running for the full UI to work. All are included in the reference `docker-compose.yml`:

| Service | Role | Default port (host) |
|---|---|---|
| **HelixObs Gateway** | Receives OTLP spans, writes entities + events to TimescaleDB, forwards traces to OTel Collector | `4317` (gRPC) |
| **TimescaleDB** | Stores entities, parent DAG, and entity events. Source for Grafana PostgreSQL panels. | `5432` |
| **OTel Collector** | Receives forwarded traces (and optionally logs) from gateway, exports to Tempo / Loki | internal |
| **Tempo** | Distributed trace storage. Source for Grafana span tree panels. | `3201` |
| **Loki** | Log aggregation. Receives logs from the sidecar collector **or** the OTel Collector logs pipeline. | `3101` |
| **Prometheus** | Scrapes `helix_events_total` and other metrics from the gateway. | `9091` |
| **Grafana** | Dashboard UI — Entity Inspector, Error Entities, custom dashboards. | `3001` |
| **Log collector sidecar** | _(sidecar mode only)_ Reads container stdout, extracts JSON fields, ships to Loki as stream labels. Reference: Grafana Alloy. | internal |

### Log collector sidecar (Grafana Alloy reference config)

Your process writes newline-delimited JSON to stdout. The sidecar reads it and promotes three fields as Loki stream labels:

```alloy
discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.docker.containers.targets
  forward_to = [loki.process.helix_json.receiver]
  refresh_interval = "5s"
}

loki.process "helix_json" {
  stage.json {
    expressions = {
      helix_instrument_id = "helix_instrument_id",
      level               = "level",
    }
  }
  stage.labels {
    values = {
      helix_instrument_id = "",
      level               = "",
    }
  }
  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint { url = "http://loki:3100/loki/api/v1/push" }
}
```

Any collector that reads container stdout and forwards to Loki works: Promtail, Fluent Bit, Vector, or the OTel Collector with a `filelog` receiver. The contract is newline-delimited JSON on stdout with `helix_instrument_id` and `level` as top-level string fields.

> **Note:** `helix_entity_id` is intentionally **not** a Loki stream label. Because each entity has a unique ID, using it as a label creates one stream per entity and quickly exhausts Loki's stream limit. Entity-scoped log queries use `| json | helix_entity_id = "..."` at query time instead.

---

## 2. Installation

```bash
pip install helixobs
```

Requires Python 3.11+. The only runtime dependencies are the OpenTelemetry SDK and its gRPC OTLP exporter — no HTTP clients, no Loki SDK, no database drivers.

**From source:**

```bash
git clone https://github.com/HelixObs/client-python
cd client-python
pip install -e .
```

---

## 3. Quickstart

```python
from helixobs import Instrument
from helixobs.logging import configure_logging
import logging

# 1. Configure JSON logging (once at startup)
configure_logging()
log = logging.getLogger("my.pipeline")

# 2. Create one Instrument per process
tel = Instrument(
    service_name="mytelescope.frb-classifier",
    instrument_id="MYTELESCOPE",
    endpoint="gateway:4317",
)

# 3. Track a pipeline entity (context manager — recommended)
with tel.create("frb-classifier", id=cand_id, parents=[block_id]) as token:
    result = classify(candidate)
    log.info("candidate classified")          # helix_entity_id auto-injected
    if result.snr > 50:
        token.add_event("helix.event.candidate_promoted", {"snr": result.snr})
# token ends here — complete() called automatically; error() on exception
```

---

## 4. Core Concepts

### Entity

An entity is any discrete data product your pipeline produces or consumes: a data block, a beam candidate, an FRB event, a calibration solution. Each entity has:

- A **unique ID** you assign (e.g. `"block-abc123"`, `"cand-def456"`)
- An **instrument ID** stamped by the client library
- A **provenance DAG** — the IDs of entities this one was derived from
- **Metadata** — arbitrary key/value attributes attached at any point
- A **creation trace** — the OTel span tree from when the entity was first created
- **Operation traces** — independent traces for each post-creation operation (see below)
- **Events** — `helix.*` span events attached across any trace
- **Logs** — all log lines emitted while any of the entity's spans are active

### Entity Operations

Some work happens on an entity *after* its creation trace has ended — often asynchronously and in separate processes. Examples: converting an FRB event to HDF5, registering it in a catalog, replicating it to offsite storage.

These are not new entities. They are **operations on an existing entity**, each running in their own independent OTel trace. The Entity Inspector shows all traces associated with an entity — the creation trace plus every operation trace — so you can drill into each one separately.

Use `tel.operate()` for these cases (see API Reference). If the entity does not yet exist when an operation arrives at the gateway, a placeholder entity is created automatically.

### Instrument

`Instrument` is the single entry point. Create one instance per process and share it. It configures the OTel `TracerProvider` and manages an in-process `TraceStore` for parent resolution.

### Token

Both `tel.create()` and `tel.operate()` return a `Token`. It is usable three ways:

- **Primitive (Layer 0):** call `.start()`, then `.complete()` or `.error()` explicitly.
- **Context manager (Layer 1):** use `with tel.create(...) as token:` — lifecycle is automatic.
- **Decorator (Layer 2):** use `@tel.create(...)` above a function — wraps the call automatically.

### Parent resolution

When you pass `parents=["block-abc123"]`, the library checks its in-process store first. If the parent was created in the same process, it becomes an OTel span link. If the parent was created in a different process, its ID is set as the `helix.parent.ids` span attribute and the gateway resolves the DAG edge server-side.

---

## 5. API Reference

### `Instrument(service_name, instrument_id, endpoint, insecure)`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `service_name` | `str` | — | OTel service name. Shown in Tempo span tree. e.g. `"chime.frb-classifier"` |
| `instrument_id` | `str` | — | Instrument identifier stamped on every entity. e.g. `"CHIME"` |
| `endpoint` | `str` | `"localhost:4317"` | OTLP gRPC endpoint of the HelixObs gateway |
| `insecure` | `bool` | `True` | Plaintext gRPC. Set `False` if the gateway has TLS. |

---

### `tel.create(stage_name, *, id, parents) → Token`

Return a `Token` for a new entity entering the pipeline. The token is **not yet started** — call `.start()`, use as a context manager, or use as a decorator.

| Parameter | Type | Description |
|---|---|---|
| `stage_name` | `str` | Pipeline stage name. Shown as the span name in Tempo. e.g. `"frb-classifier"` |
| `id` | `str \| Callable` | Unique entity ID, or a callable that receives the wrapped function's args and returns one. |
| `parents` | `list[str] \| Callable \| None` | Parent entity IDs for the provenance DAG, or a callable. |

**Layer 0 — Primitive:**
```python
token = tel.create("frb-classifier", id=cand_id, parents=[block_id]).start()
result = classify(candidate)
token.complete()

# On failure:
token.error(metadata={"message": "SNR below threshold", "snr": result.snr})
```

**Layer 1 — Context manager:**
```python
with tel.create("frb-classifier", id=cand_id, parents=[block_id]) as token:
    result = classify(candidate)
    token.add_event("helix.event.candidate_promoted", {"snr": result.snr})
```

**Layer 2 — Decorator:**
```python
@tel.create(
    "frb-classifier",
    id=lambda cand: cand.id,
    parents=lambda cand: [cand.block_id],
)
def classify_candidate(cand):
    return run_classifier(cand)
```

---

### `tel.operate(operation, *, entity_id) → Token`

Return a `Token` for an operation on an existing entity. Always starts a new root OTel trace — independent of the entity's creation trace. The gateway writes an `entity_operations` row so the Entity Inspector shows all traces for that entity across its lifetime.

| Parameter | Type | Description |
|---|---|---|
| `operation` | `str` | Operation name shown in Tempo and the Trace panel |
| `entity_id` | `str` | ID of the existing entity being operated on |

```python
with tel.operate("hdf5-conversion", entity_id=event_id) as op:
    size_mb = write_hdf5(event_id)
    op.set_attribute("helix.chime.hdf5_size_mb", size_mb)
    if write_failed:
        log.error("HDF5 write failed: disk full")
        op.error({"message": "hdf5_write_error"})
        return None
    log.info(f"HDF5 written: {size_mb:.1f} MB")
```

Unhandled exceptions inside the `with` block automatically mark the operation as failed.

If the entity does not exist yet (race condition or late arrival), the gateway creates a minimal placeholder entity so the operation is never orphaned.

---

### `Token` methods

| Method | Description |
|---|---|
| `token.start() → Token` | Start the underlying span. Returns `self` for chaining. Called implicitly by the context manager and decorator. |
| `token.complete(metadata=None)` | End the span successfully. Optional `metadata` dict is set as span attributes. No-op if already done. |
| `token.error(metadata=None)` | Record a `helix.error` span event, set span status to ERROR, and end the span. No-op if already done. |
| `token.add_event(name, attributes=None)` | Attach a timestamped `helix.*` event to the span. Written to `entity_events` by the gateway. |
| `token.set_attribute(key, value)` | Set a span attribute directly. |

---

### `tel.child_span(name, *, parent_id=None, attributes=None)`

Create a child span within an entity's current trace **without registering a new entity**. Use for internal sub-steps that should appear in Tempo's span tree (e.g. RFI mitigation, dedispersion, per-beam clustering) but don't need their own provenance node.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Span name shown in Tempo |
| `parent_id` | `str \| None` | Entity ID to parent to. Works even after that entity's token is completed. Defaults to the current active context. |
| `attributes` | `dict \| None` | OTel span attributes to set immediately |

```python
with tel.child_span("rfi-mitigation") as span:
    n_flagged = excise_rfi(data)
    span.set_attribute("helix.chime.rfi_flagged_channels", n_flagged)
    log.info(f"RFI mitigation: {n_flagged} channels excised")

# Parent to a completed entity from a different thread
with tel.child_span("ring-buffer-rpc", parent_id=event_id) as span:
    data = fetch_ring_buffer()
    span.set_attribute("helix.chime.buffer_size_mb", len(data) / 1e6)
```

---

### `tel.shutdown()`

Flush all pending spans and shut down the OTLP exporter. Call at process exit to avoid losing the last batch.

```python
import atexit
atexit.register(tel.shutdown)
```

---

### `configure_logging(*, otlp=False)`  _(helixobs.logging)_

Install the helix log-record factory and attach log handlers on the root logger. Call once at startup, before any `logging.getLogger()` calls.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `otlp` | `bool` | `False` | When `True`, ship logs via OTLP gRPC to the OTel Collector instead of writing JSON to stdout. |

**Sidecar mode (default):** logs go to stdout as newline-delimited JSON. A Grafana Alloy sidecar (or any log collector) picks them up and ships to Loki.

```python
from helixobs.logging import configure_logging
configure_logging()          # stdout JSON — Alloy sidecar required
```

**OTLP mode:** logs are shipped directly to the OTel Collector over gRPC on the same port as traces (`OTEL_EXPORTER_OTLP_ENDPOINT`, default `http://localhost:4317`). No sidecar required. Use this for greenfield services.

```python
configure_logging(otlp=True) # OTLP gRPC → OTel Collector → Loki
```

In both modes every log line carries the same helix context fields (populated from the active OTel span):

```json
{"ts": "2026-04-18T12:00:01", "level": "info", "logger": "chime.simulator", "msg": "candidate classified", "helix_entity_id": "cand-abc123", "helix_instrument_id": "CHIME", "otel_trace_id": "abc...", "otel_span_id": "def..."}
```

**Log while the span is active** (before `token.complete()`/`token.error()`) to capture entity context.

Optional environment variables for GitHub source links:

| Variable | Description |
|---|---|
| `GITHUB_REPO` | e.g. `https://github.com/myorg/myrepo` |
| `GIT_COMMIT_SHA` | e.g. `a1b2c3d4` |

When both are set, the `src` field in each log line becomes a full GitHub permalink.

---

## 6. Integration Patterns

### Pattern 1 — Primitive (Layer 0)

Best for long-running loops, async code, or anywhere a context manager is awkward.

```python
block_id = f"block-{uuid.uuid4().hex[:12]}"
token = tel.create("x-engine", id=block_id).start()

try:
    data = ingest_fpga_block()
    log.info("block ingested")
    token.complete()
except Exception as exc:
    log.exception("ingestion failed")
    token.error(metadata={"message": str(exc)})
```

### Pattern 2 — Context manager (Layer 1)

Best for structured Python code where one `with` block maps to one pipeline stage.

```python
with tel.create("frb-classifier", id=cand_id, parents=[block_id]) as token:
    result = classifier.run(candidate)
    log.info("candidate classified")
    if result.snr > 50:
        token.add_event("helix.event.candidate_promoted", {"snr": result.snr})
# complete() on clean exit; error() on unhandled exception
```

### Pattern 3 — Decorator (Layer 2)

Best when one function maps cleanly to one pipeline stage and its arguments carry the entity ID.

```python
@tel.create(
    "clustering",
    id=lambda cands: f"event-{uuid.uuid4().hex[:12]}",
    parents=lambda cands: [c.id for c in cands],
)
def cluster_event(cands: list[Candidate]) -> FRBEvent:
    return run_clustering(cands)
```

### Internal sub-steps with `child_span()`

Use `child_span()` for work that belongs inside one entity's trace — it appears in Tempo as a nested span but creates no DB entity row.

```python
token = tel.create("l1-search", id=beam_id, parents=[block_id]).start()

with tel.child_span("rfi-mitigation") as span:
    n = excise_rfi(data)
    span.set_attribute("helix.chime.rfi_flagged_channels", n)

with tel.child_span("dedispersion") as span:
    trials = run_dedispersion(data)
    span.set_attribute("helix.chime.dm_trials", trials)

token.complete()
```

### Post-creation operations with `operate()`

Use `operate()` for work that happens on an entity after its creation trace has ended — even asynchronously in a separate process.

```python
# These run after the FRB event entity is fully created and its trace is closed.
# Each gets its own independent trace, all linked to the same event_id.

with tel.operate("hdf5-conversion", entity_id=event_id) as op:
    size_mb = write_hdf5(event_id)
    op.set_attribute("helix.chime.hdf5_size_mb", size_mb)
    log.info(f"HDF5 written: {size_mb:.1f} MB")

with tel.operate("registration", entity_id=event_id) as op:
    register_in_catalog(event_id)
    op.set_attribute("helix.chime.registration_status", "ok")
    log.info("event registered")

for dest in ["cedar", "niagara"]:
    with tel.operate("replication", entity_id=event_id) as op:
        op.set_attribute("helix.chime.replication_dest", dest)
        if not replicate_to(dest):
            log.error(f"replication to {dest} failed")
            op.error({"message": "replication_timeout"})
```

### Multi-stage pipeline

Each stage creates its own entity. Parent IDs wire the provenance DAG.

```python
# Stage 1: ingest
with tel.create("x-engine", id=block_id) as token:
    ingest()
    log.info("block ingested")

# Stage 2: classify (derived from the block)
with tel.create("frb-classifier", id=cand_id, parents=[block_id]) as token:
    result = classify()
    log.info("candidate classified")

# Stage 3: cluster (derived from multiple candidates)
with tel.create("clustering", id=event_id, parents=cand_ids) as token:
    event = cluster()
    log.info("event clustered")
```

The gateway stores `parent_ids` for each entity, enabling the recursive provenance DAG query in Grafana.

---

## 7. Structured Logging

`configure_logging()` installs a JSON formatter. Every `logging` call on any logger emits a JSON object — written to stdout in sidecar mode, or shipped via OTLP gRPC in OTLP mode. No configuration required beyond the one-time call.

**Log while the span is active** to capture entity context automatically:

```python
# CORRECT — log before complete(); factory reads entity ID from active span
token = tel.create("frb-classifier", id=cand_id, parents=[block_id]).start()
result = classify()
log.info("candidate classified")     # helix_entity_id present in output
token.complete()

# WRONG — span is already ended; factory finds no active span
token = tel.create("frb-classifier", id=cand_id, parents=[block_id]).start()
result = classify()
token.complete()
log.info("candidate classified")     # helix_entity_id missing
```

With the context manager pattern this is automatic — the span is active for the entire `with` block.

**JSON log line fields:**

| Field | Source | Notes |
|---|---|---|
| `ts` | Log record | ISO 8601 timestamp |
| `level` | Log record | `info`, `warning`, `error`, `debug` |
| `logger` | Log record | Logger name |
| `msg` | Log record | Log message |
| `src` | File + line | Repo-relative path, or GitHub permalink |
| `helix_entity_id` | Active OTel span | Empty if no active span |
| `helix_instrument_id` | Active OTel span | Empty if no active span |
| `otel_trace_id` | Active OTel span | 32-hex trace ID |
| `otel_span_id` | Active OTel span | 16-hex span ID |
| `exc` | Exception info | Only present when `log.exception()` / `exc_info=True` |

Fields with empty values are omitted from the output to keep lines short.

---

## 8. Data Model

### entities table

| Column | Type | Description |
|---|---|---|
| `id` | TEXT | Entity ID (your string) |
| `instrument_id` | TEXT | Instrument identifier |
| `trace_id` | TEXT | OTel trace ID linking to Tempo |
| `timestamp_ns` | BIGINT | Span start time in nanoseconds |
| `parent_ids` | TEXT[] | Parent entity IDs (provenance DAG edges) |
| `metadata` | JSONB | All `helix.*` span attributes |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time (hypertable partition key) |

### entity_events table

One row per `helix.*` span event, from any trace associated with the entity (creation or operation).

| Column | Type | Description |
|---|---|---|
| `entity_id` | TEXT | References entities.id |
| `instrument_id` | TEXT | Instrument identifier |
| `event_name` | TEXT | e.g. `helix.error`, `helix.event.candidate_promoted` |
| `timestamp_ns` | BIGINT | When the event occurred (from OTel span event) |
| `trace_id` | TEXT | OTel trace ID — links event to the span that emitted it |
| `metadata` | JSONB | Event attributes from `token.add_event(name, attributes)` |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time |

### entity_operations table

One row per `tel.operate()` call. Links independent post-creation traces back to their entity.

| Column | Type | Description |
|---|---|---|
| `entity_id` | TEXT | The entity being operated on |
| `instrument_id` | TEXT | Instrument identifier |
| `operation` | TEXT | Operation name (e.g. `hdf5-conversion`, `registration`, `replication`) |
| `trace_id` | TEXT | OTel trace ID for this operation — click to open in Tempo |
| `timestamp_ns` | BIGINT | Operation start time in nanoseconds |
| `metadata` | JSONB | Span attributes set inside the `operate()` block |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time |

An entity can have any number of operation rows — there is no uniqueness constraint on `(entity_id, operation)`.

Data is retained for 90 days and auto-compressed after 7 days (TimescaleDB policies).

---

## 9. Complete Example

The CHIME mock telescope (`mock-telescope/simulate.py`) is the reference implementation. The pattern below is the recommended structure for any new instrument:

```python
"""
my_pipeline.py — HelixObs integration for MyTelescope.

Environment variables:
    GATEWAY_ENDPOINT   HelixObs gateway gRPC address (default: localhost:4317)
"""

import logging
import os
import uuid

from helixobs import Instrument
from helixobs.logging import configure_logging

# ── One-time startup ──────────────────────────────────────────────────────────

configure_logging()
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("mytelescope.pipeline")

tel = Instrument(
    service_name="mytelescope.pipeline",
    instrument_id="MYTELESCOPE",
    endpoint=os.environ.get("GATEWAY_ENDPOINT", "localhost:4317"),
)

# ── Pipeline stages ───────────────────────────────────────────────────────────

def ingest_block() -> str:
    block_id = f"block-{uuid.uuid4().hex[:12]}"

    with tel.create("x-engine", id=block_id) as token:
        data = read_fpga_buffer()
        log.info("block ingested")
        token.add_event("helix.event.block_received", {"n_samples": len(data)})

    return block_id


def classify_candidate(block_id: str, beam: int) -> str | None:
    cand_id = f"cand-{uuid.uuid4().hex[:12]}"
    token = tel.create("frb-classifier", id=cand_id, parents=[block_id]).start()

    try:
        result = run_classifier(beam)
        if result.snr < 8.0:
            log.warning("candidate rejected: low SNR")
            token.error(metadata={"message": "SNR below threshold", "snr": result.snr})
            return None

        log.info("candidate classified")
        token.complete()
        return cand_id

    except Exception as exc:
        log.exception("classifier failed")
        token.error(metadata={"message": str(exc)})
        return None


def cluster_event(cand_ids: list[str]) -> str | None:
    if len(cand_ids) < 2:
        return None

    event_id = f"frb-{uuid.uuid4().hex[:12]}"
    with tel.create("clustering", id=event_id, parents=cand_ids) as token:
        event = run_clustering(cand_ids)
        log.info("event clustered")
        token.add_event("helix.event.frb_candidate", {
            "ra":             event.ra,
            "dec":            event.dec,
            "classification": event.classification,
        })

    return event_id


def post_detection(event_id: str) -> None:
    with tel.operate("hdf5-conversion", entity_id=event_id) as op:
        size_mb = write_hdf5(event_id)
        op.set_attribute("helix.chime.hdf5_size_mb", size_mb)
        log.info(f"HDF5 written: {size_mb:.1f} MB")

    with tel.operate("registration", entity_id=event_id) as op:
        register_in_catalog(event_id)
        log.info("event registered")

    for dest in ["cedar", "niagara"]:
        with tel.operate("replication", entity_id=event_id) as op:
            op.set_attribute("helix.chime.replication_dest", dest)
            if not replicate_to(dest):
                log.error(f"replication to {dest} timed out")
                op.error({"message": "replication_timeout", "dest": dest})
```

---

### Grafana dashboards

Once data is flowing, two dashboards are available out of the box:

| Dashboard | URL | Description |
|---|---|---|
| **Entity Inspector** | `http://localhost:3001/d/helix-entity-inspector` | Enter any entity ID. Shows provenance DAG, metadata, events, span tree, and correlated logs. |
| **Error Entities** | `http://localhost:3001/d/helix-error-entities` | Table of all failed entities with error rate chart. Click any row to open Entity Inspector. |
