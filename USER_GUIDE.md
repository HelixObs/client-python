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
| **HelixObs Gateway** | gRPC endpoint (default `localhost:4317`). Every `track()`/`complete()`/`error()` call sends an OTLP span here. |
| **Log collector sidecar** | Reads your process's stdout, parses JSON, and ships logs to Loki. Grafana Alloy is the reference implementation (see below). Any collector that reads container stdout works. |

> **That's it.** Your pipeline process makes no HTTP calls, no database connections, and no direct Loki calls. All telemetry flows through gRPC spans + structured stdout.

### HelixObs stack components

The following services must be running for the full UI to work. All are included in the reference `docker-compose.yml`:

| Service | Role | Default port (host) |
|---|---|---|
| **HelixObs Gateway** | Receives OTLP spans, writes entities + events to TimescaleDB, forwards traces to OTel Collector | `4317` (gRPC) |
| **TimescaleDB** | Stores entities, parent DAG, and entity events. Source for Grafana PostgreSQL panels. | `5432` |
| **OTel Collector** | Receives forwarded traces from gateway, exports to Tempo | internal |
| **Tempo** | Distributed trace storage. Source for Grafana span tree panels. | `3201` |
| **Loki** | Log aggregation. Receives logs from the sidecar collector. | `3101` |
| **Prometheus** | Scrapes `helix_events_total` and other metrics from the gateway. | `9091` |
| **Grafana** | Dashboard UI — Entity Inspector, Error Entities, custom dashboards. | `3001` |
| **Log collector sidecar** | Reads container stdout, extracts JSON fields, ships to Loki as stream labels. Reference: Grafana Alloy. | internal |

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
      helix_entity_id     = "helix_entity_id",
      helix_instrument_id = "helix_instrument_id",
      level               = "level",
    }
  }
  stage.labels {
    values = {
      helix_entity_id     = "",
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

Any collector that reads container stdout and forwards to Loki works: Promtail, Fluent Bit, Vector, or the OTel Collector with a `filelog` receiver. The contract is newline-delimited JSON on stdout with `helix_entity_id`, `helix_instrument_id`, and `level` as top-level string fields.

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
with tel.stage("frb-classifier", id=cand_id, parents=[block_id]) as span:
    result = classify(candidate)
    log.info("candidate classified")          # helix_entity_id auto-injected
    if result.snr > 50:
        span.add_event("helix.event.candidate_promoted", {"snr": result.snr})
# span ends here — complete() called automatically; error() on exception
```

---

## 4. Core Concepts

### Entity

An entity is any discrete data product your pipeline produces or consumes: a data block, a beam candidate, an FRB event, a calibration solution. Each entity has:

- A **unique ID** you assign (e.g. `"block-abc123"`, `"cand-def456"`)
- An **instrument ID** stamped by the client library
- A **provenance DAG** — the IDs of entities this one was derived from
- **Metadata** — arbitrary key/value attributes attached at any point
- An **OTel trace** — the distributed span tree for the processing pipeline
- **Logs** — all log lines emitted while the entity's span was active

### Instrument

`Instrument` is the single entry point. Create one instance per process and share it. It configures the OTel `TracerProvider` and manages an in-process `TraceStore` for parent resolution.

### Token

`track()` returns a `Token`. Pass it to `complete()` or `error()` when processing is done. When using the context manager (`stage()`), the token is the `as` variable and lifecycle is automatic.

### Parent resolution

When you pass `parents=["block-abc123"]`, the library checks its in-process store first. If the parent was tracked in the same process, it becomes an OTel span link. If the parent was created in a different process, its ID is set as the `helix.parent.ids` span attribute and the gateway resolves the DAG edge server-side.

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

### `tel.track(stage_name, *, id, parents) → Token`

Start tracking an entity. Opens an OTel span and sets it as the active context on the calling thread.

| Parameter | Type | Description |
|---|---|---|
| `stage_name` | `str` | Pipeline stage name. e.g. `"frb-classifier"`. Shown as the span name in Tempo. |
| `id` | `str` | Unique entity ID. You own this — any string that uniquely identifies the data product. |
| `parents` | `list[str]` | Optional list of parent entity IDs. Builds the provenance DAG. |

```python
token = tel.track("frb-classifier", id=cand_id, parents=[block_id])
result = classify(candidate)
tel.complete(token)
```

---

### `tel.complete(token, metadata=None)`

Mark the entity as successfully processed. Ends the span. Safe to call multiple times (no-op after first call).

```python
tel.complete(token, metadata={"output_file": "/data/frb-001.hdf5"})
```

---

### `tel.error(token, metadata=None)`

Record a `helix.error` span event and mark the entity as failed. Sets OTel span status to `ERROR`. Safe to call multiple times.

```python
tel.error(token, metadata={"message": "SNR threshold not met", "snr": 7.2})
```

---

### `tel.stage(stage_name, *, id, parents) → _StageHelper`

Returns a helper usable as a **context manager** or **decorator**. On exception, calls `error()` automatically. On normal exit, calls `complete()`.

**Context manager:**

```python
with tel.stage("frb-classifier", id=cand_id, parents=[block_id]) as span:
    result = classify(candidate)
    span.add_event("helix.event.candidate_promoted", {"snr": result.snr})
```

**Decorator** (when `id` and `parents` can be derived from function arguments):

```python
@tel.stage(
    "frb-classifier",
    id=lambda cand: cand.id,
    parents=lambda cand: [cand.block_id],
)
def classify_candidate(cand):
    return run_classifier(cand)
```

---

### `token.add_event(name, attributes=None)`

Attach a timestamped event to the entity's span. Use:

- `"helix.error"` — for errors (prefer `tel.error()` instead)
- `"helix.event.<name>"` — for scientifically notable signals

```python
span.add_event("helix.event.voevent_issued", {
    "ra":  result.ra,
    "dec": result.dec,
    "dm":  result.dm,
})
```

---

### `tel.shutdown()`

Flush all pending spans and shut down the OTLP exporter. Call at process exit to avoid losing the last batch.

```python
import atexit
atexit.register(tel.shutdown)
```

---

### `configure_logging()`  _(helixobs.logging)_

Install the helix log-record factory and JSON formatter on the root logger. Call once at startup, before any `logging.getLogger()` calls.

```python
from helixobs.logging import configure_logging
configure_logging()
```

Every log line becomes a single-line JSON object written to stdout:

```json
{"ts": "2026-04-18T12:00:01", "level": "info", "logger": "chime.simulator", "msg": "candidate classified", "helix_entity_id": "cand-abc123", "helix_instrument_id": "CHIME", "otel_trace_id": "abc...", "otel_span_id": "def..."}
```

Fields are auto-populated from the active OTel span — **log while the span is active** (before `complete()`/`error()`) to capture entity context.

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
token = tel.track("x-engine", id=block_id)

try:
    data = ingest_fpga_block()
    log.info("block ingested")
    tel.complete(token)
except Exception as exc:
    log.exception("ingestion failed")
    tel.error(token, metadata={"message": str(exc)})
```

### Pattern 2 — Context manager (Layer 1)

Best for structured Python code where one `with` block maps to one pipeline stage.

```python
with tel.stage("frb-classifier", id=cand_id, parents=[block_id]) as span:
    result = classifier.run(candidate)
    log.info("candidate classified")
    if result.snr > 50:
        span.add_event("helix.event.candidate_promoted", {"snr": result.snr})
# complete() on clean exit; error() on unhandled exception
```

### Pattern 3 — Decorator (Layer 2)

Best when one function maps cleanly to one pipeline stage and its arguments carry the entity ID.

```python
@tel.stage(
    "clustering",
    id=lambda cands: f"event-{uuid.uuid4().hex[:12]}",
    parents=lambda cands: [c.id for c in cands],
)
def cluster_event(cands: list[Candidate]) -> FRBEvent:
    return run_clustering(cands)
```

### Multi-stage pipeline

Each stage creates its own entity. Parent IDs wire the provenance DAG.

```python
# Stage 1: ingest
block_token = tel.track("x-engine", id=block_id)
ingest()
log.info("block ingested")
tel.complete(block_token)

# Stage 2: classify (derived from the block)
cand_token = tel.track("frb-classifier", id=cand_id, parents=[block_id])
result = classify()
log.info("candidate classified")
tel.complete(cand_token)

# Stage 3: cluster (derived from multiple candidates)
event_token = tel.track("clustering", id=event_id, parents=cand_ids)
event = cluster()
log.info("event clustered")
tel.complete(event_token)
```

The gateway stores `parent_ids` for each entity, enabling the recursive provenance DAG query in Grafana.

---

## 7. Structured Logging

`configure_logging()` installs a JSON formatter. Every `logging` call on any logger emits a JSON object to stdout. No configuration required beyond the one-time call.

**Log while the span is active** to capture entity context automatically:

```python
# CORRECT — log before complete(); factory reads entity ID from active span
token = tel.track("frb-classifier", id=cand_id, parents=[block_id])
result = classify()
log.info("candidate classified")     # helix_entity_id present in output
tel.complete(token)

# WRONG — span is already ended; factory finds no active span
token = tel.track("frb-classifier", id=cand_id, parents=[block_id])
result = classify()
tel.complete(token)
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

| Column | Type | Description |
|---|---|---|
| `entity_id` | TEXT | References entities.id |
| `instrument_id` | TEXT | Instrument identifier |
| `event_name` | TEXT | e.g. `helix.error`, `helix.event.candidate_promoted` |
| `timestamp_ns` | BIGINT | When the event occurred (from OTel span event) |
| `metadata` | JSONB | Event attributes from `span.add_event(name, attributes)` |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time |

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

    with tel.stage("x-engine", id=block_id) as span:
        data = read_fpga_buffer()
        log.info("block ingested")
        span.add_event("helix.event.block_received", {"n_samples": len(data)})

    return block_id


def classify_candidate(block_id: str, beam: int) -> str | None:
    cand_id = f"cand-{uuid.uuid4().hex[:12]}"
    token = tel.track("frb-classifier", id=cand_id, parents=[block_id])

    try:
        result = run_classifier(beam)
        if result.snr < 8.0:
            log.warning("candidate rejected: low SNR")
            tel.error(token, metadata={"message": "SNR below threshold", "snr": result.snr})
            return None

        log.info("candidate classified")
        tel.complete(token)
        return cand_id

    except Exception as exc:
        log.exception("classifier failed")
        tel.error(token, metadata={"message": str(exc)})
        return None


def cluster_event(cand_ids: list[str]) -> None:
    if len(cand_ids) < 2:
        return

    event_id = f"frb-{uuid.uuid4().hex[:12]}"
    with tel.stage("clustering", id=event_id, parents=cand_ids) as span:
        event = run_clustering(cand_ids)
        log.info("event clustered")
        span.add_event("helix.event.frb_candidate", {
            "ra":             event.ra,
            "dec":            event.dec,
            "classification": event.classification,
        })
```

---

### Grafana dashboards

Once data is flowing, two dashboards are available out of the box:

| Dashboard | URL | Description |
|---|---|---|
| **Entity Inspector** | `http://localhost:3001/d/helix-entity-inspector` | Enter any entity ID. Shows provenance DAG, metadata, events, span tree, and correlated logs. |
| **Error Entities** | `http://localhost:3001/d/helix-error-entities` | Table of all failed entities with error rate chart. Click any row to open Entity Inspector. |
