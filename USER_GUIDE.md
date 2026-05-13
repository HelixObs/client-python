# HelixObs Python Client — User Guide

HelixObs gives scientific instrument pipelines entity-centric observability: every data product gets a persistent identity, a provenance graph, structured logs, and a distributed trace — all queryable from a single Grafana UI.

---

## Table of Contents

1. [Required Infrastructure](#1-required-infrastructure)
2. [Installation](#2-installation)
3. [Core Concepts](#3-core-concepts)
4. [API Reference](#4-api-reference)
5. [Integration Patterns](#5-integration-patterns)
6. [Structured Logging](#6-structured-logging)
7. [Data Model](#7-data-model)
8. [Complete Example](#8-complete-example)

---

## 1. Required Infrastructure

Your pipeline process connects to a single endpoint. The full HelixObs stack runs everything else.

| Requirement | Details |
|---|---|
| **HelixObs Gateway** | gRPC endpoint (default `localhost:4317`). Every `create()`/`operate()` call sends an OTLP span here. |
| **OTel Collector** | gRPC endpoint (default `localhost:4319`). Required for OTLP log shipping — the gateway handles traces only. |
| **Log delivery** | Sidecar mode (default) or OTLP mode — see [Structured Logging](#6-structured-logging). |

> In both modes your pipeline makes no HTTP calls and no direct database calls. All telemetry flows through gRPC.

> **Two endpoints, not one.** The gateway (`4317`) handles entity traces. The OTel Collector (`4319`) handles logs. The `setup()` function wires both with a single `service_name` — use it unless you need fine-grained control.

### Stack components

| Service | Role | Default port |
|---|---|---|
| **Gateway** | Receives OTLP spans, writes entities + events to TimescaleDB, forwards to OTel Collector | `4317` gRPC, `8080` HTTP |
| **TimescaleDB** | Stores entities, provenance DAG, events, and operation records | `5432` |
| **OTel Collector** | Receives forwarded traces and logs, exports to Tempo and Loki | internal |
| **Tempo** | Distributed trace storage | `3201` |
| **Loki** | Log aggregation | `3101` |
| **Prometheus** | Metrics — gateway throughput, error rates, DB latency | `9091` |
| **Grafana** | Dashboard UI — Entity Inspector, Error Entities, Platform Health | `3001` |

All services are included in the reference `docker-compose.yml`.

---

## 2. Installation

```bash
pip install helixobs
```

Requires Python 3.11+. Runtime dependencies: OpenTelemetry SDK and its gRPC OTLP exporter — no HTTP clients, no database drivers.

**From source:**

```bash
git clone https://github.com/HelixObs/client-python
cd client-python
pip install -e .
```

---

## 3. Core Concepts

### Entity

An entity is any discrete data product your pipeline produces or consumes: a raw observation, an intermediate product, a derived result, a calibration solution. Each entity has:

- A **unique ID** you assign (any string that identifies the data product)
- An **instrument ID** stamped by the client library
- A **provenance DAG** — the IDs of entities this one was derived from
- **Metadata** — key/value attributes attached at any point
- A **creation trace** — the OTel span tree from when the entity was first created
- **Operation traces** — independent traces for each post-creation operation
- **Events** — `helix.*` span events recorded across any trace
- **Logs** — all log lines emitted while any of the entity's spans are active

### Entity Operations

Some work happens on an entity *after* its creation trace has ended — often asynchronously in a separate process. These are not new entities; they are **operations on an existing entity**, each with their own independent OTel trace. Use `tel.operate()` for these cases.

If the entity does not yet exist when an operation arrives (race condition or late arrival), the gateway creates a minimal placeholder automatically.

### Token

Both `tel.create()` and `tel.operate()` return a `Token`. It is usable three ways — all emit identical OTLP:

| Layer | Style | Use when |
|---|---|---|
| 0 — Primitive | `.start()` / `.complete()` / `.error()` | Long-running loops, async code, awkward control flow |
| 1 — Context manager | `with tel.create(...) as token:` | Structured code where one block = one stage |
| 2 — Decorator | `@tel.create(...)` | Functions that map cleanly to one stage |

### Parent resolution

When you pass `parents=["input-abc"]`, the library checks its in-process store first. If the parent was created in the same process, it becomes an OTel span `Link`. If it was created in a different process, its ID is set as the `helix.parent.ids` attribute and the gateway resolves the edge server-side.

---

## 4. API Reference

### `setup(service_name, *, instrument_id, endpoint, insecure, otlp, log_endpoint, process_name) → Instrument`

The recommended entry point. Configures logging and returns a ready-to-use `Instrument` — both stamped with the same `service_name`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `service_name` | `str` | — | OTel service name for traces and logs. e.g. `"chime.l1"` |
| `instrument_id` | `str` | — | Instrument identifier stamped on every entity span |
| `endpoint` | `str` | `"localhost:4317"` | Gateway gRPC address for traces |
| `insecure` | `bool` | `True` | Plaintext gRPC |
| `otlp` | `bool` | `False` | Ship logs via OTLP. When `False`, JSON is written to stdout |
| `log_endpoint` | `str \| None` | `None` | OTel Collector gRPC address for logs. Falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` env var, then `http://localhost:4319` |
| `process_name` | `str \| None` | `None` | Loki stream label for the Pipeline Process Logs dashboard. See [Process naming convention](#process-naming-convention). |

```python
from helixobs import setup

tel = setup("chime.l1", instrument_id="CHIME", endpoint="gateway:4317", otlp=True,
            process_name="CHIME/l1-search")
```

---

### `Instrument(service_name, instrument_id, endpoint, insecure)`

Create one instance per process and share it across pipeline stages.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `service_name` | `str` | — | OTel service name, shown in Tempo. e.g. `"my-instrument.pipeline"` |
| `instrument_id` | `str` | — | Identifier stamped on every entity. e.g. `"MY_INSTRUMENT"` |
| `endpoint` | `str` | `"localhost:4317"` | OTLP gRPC address of the HelixObs gateway |
| `insecure` | `bool` | `True` | Plaintext gRPC. Set `False` if the gateway uses TLS. |

---

### `tel.create(stage_name, *, id, parents) → Token`

Return a `Token` for a new entity entering the pipeline.

| Parameter | Type | Description |
|---|---|---|
| `stage_name` | `str` | Pipeline stage name, shown as the span name in Tempo |
| `id` | `str \| Callable` | Unique entity ID, or a callable receiving the wrapped function's args |
| `parents` | `list[str] \| Callable \| None` | Parent entity IDs for the provenance DAG, or a callable |

```python
# Layer 0 — Primitive
token = tel.create("detector", id=product_id, parents=[frame_id]).start()
result = process(item)
token.complete()                                     # or token.error({"message": "..."})

# Layer 1 — Context manager
with tel.create("detector", id=product_id, parents=[frame_id]) as token:
    result = process(item)
    token.add_event("helix.event.detection_confirmed", {"score": result.score})

# Layer 2 — Decorator
@tel.create("detector", id=lambda item: item.id, parents=lambda item: [item.frame_id])
def detect(item):
    return run_detector(item)
```

---

### `tel.operate(operation, *, entity_id) → Token`

Return a `Token` for an operation on an existing entity. Always starts a new root OTel trace independent of the entity's creation trace. The gateway writes an `entity_operations` row so the Entity Inspector shows all traces for that entity across its lifetime.

| Parameter | Type | Description |
|---|---|---|
| `operation` | `str` | Operation name shown in Tempo and the Entity Inspector |
| `entity_id` | `str` | ID of the existing entity being operated on |

```python
with tel.operate("archival", entity_id=result_id) as op:
    size_mb = write_archive(result_id)
    op.set_attribute("myinstrument.archive_size_mb", size_mb)
    log.info(f"archive written: {size_mb:.1f} MB")
```

Unhandled exceptions inside the `with` block automatically call `op.error()`.

---

### `Token` methods

| Method | Description |
|---|---|
| `token.start() → Token` | Start the span. Returns `self` for chaining. Called implicitly by context manager and decorator. |
| `token.complete(metadata=None)` | End the span successfully. Optional `metadata` dict is set as span attributes. No-op after first call. |
| `token.error(metadata=None)` | Record a `helix.error` span event, set span status to ERROR, and **end the span**. No-op after first call. |
| `token.add_error(metadata=None)` | Record a `helix.error` span event and set span status to ERROR **without ending the span**. Use for recoverable failures where the operation should continue. |
| `token.add_event(name, attributes=None)` | Attach a timestamped `helix.*` event. Written to `entity_events` by the gateway. |
| `token.set_attribute(key, value)` | Set a span attribute directly. |

---

### `tel.child_span(name, *, parent_id=None, attributes=None)`

Create a child span within an entity's current trace **without registering a new entity**. Use for internal sub-steps that should appear in Tempo's span tree but don't need a provenance node.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Span name shown in Tempo |
| `parent_id` | `str \| None` | Entity ID to parent to. Works even after that entity's token is completed. Defaults to current active context. |
| `attributes` | `dict \| None` | Span attributes to set immediately |

```python
token = tel.create("processor", id=product_id, parents=[frame_id]).start()

with tel.child_span("quality-filter") as span:
    n_flagged = run_quality_filter(data)
    span.set_attribute("myinstrument.flagged_channels", n_flagged)

with tel.child_span("signal-processing") as span:
    trials = run_signal_processing(data)
    span.set_attribute("myinstrument.trials", trials)

token.complete()
```

---

### `tel.shutdown()`

Flush pending spans and shut down the OTLP exporter. Call at process exit to avoid losing the last batch.

```python
import atexit
atexit.register(tel.shutdown)
```

---

### `configure_logging(*, otlp=False, service_name=None)`  _(helixobs.logging)_

Install the helix log-record factory. Prefer `setup()` for the common case — use this directly only when you need logs without traces.

| Mode | How to enable | Log delivery |
|---|---|---|
| Sidecar (default) | `configure_logging()` | JSON to stdout — a log collector (e.g. Grafana Alloy) ships to Loki |
| OTLP | `configure_logging(otlp=True, service_name="my.service")` | Logs shipped via gRPC to the OTel Collector — no sidecar required |

`service_name` is **required** when `otlp=True` — raises `ValueError` if omitted. When `Instrument` is also used, it overwrites the log provider resource with its own `service_name`.

In both modes every log line carries helix context fields injected from the active OTel span:

```json
{"ts": "2026-04-18T12:00:01", "level": "info", "logger": "my.pipeline", "msg": "item processed",
 "helix_entity_id": "product-abc123", "helix_instrument_id": "MY_INSTRUMENT",
 "otel_trace_id": "abc...", "otel_span_id": "def..."}
```

Optional environment variables for GitHub source links in log lines:

| Variable | Description |
|---|---|
| `GITHUB_REPO` | e.g. `https://github.com/myorg/myrepo` |
| `GIT_COMMIT_SHA` | e.g. `a1b2c3d4` |

---

## 5. Integration Patterns

### N-to-1 provenance

A single entity can have multiple parents from independent processing chains. Standard OTel tracing cannot model this — it is the defining feature of HelixObs.

```python
# Process N partitions independently
for part_id in partition_ids:
    with tel.create("partition-processor", id=f"part-{part_id}", parents=[frame_id]):
        process_partition(part_id)

# Aggregate into one result derived from all N
with tel.create("aggregator", id=result_id, parents=[f"part-{p}" for p in partition_ids]) as token:
    result = aggregate(partition_ids)
```

The gateway resolves all parent IDs into OTel span links, so Tempo shows the full DAG.

### Post-creation operations

Operations run after an entity's creation trace has closed — even in a separate process. Each gets its own independent trace, all linked to the same entity.

```python
with tel.operate("archival", entity_id=result_id) as op:
    size_mb = write_archive(result_id)
    op.set_attribute("myinstrument.archive_size_mb", size_mb)
    log.info(f"archive written: {size_mb:.1f} MB")

with tel.operate("catalog-registration", entity_id=result_id) as op:
    register(result_id)
    log.info("result registered in catalog")

for site in ["site-a", "site-b"]:
    with tel.operate("replication", entity_id=result_id) as op:
        op.set_attribute("myinstrument.replication_dest", site)
        if not replicate_to(site):
            log.error(f"replication to {site} timed out")
            op.error({"message": "replication_timeout", "dest": site})
```

### Recoverable failures

Use `add_error()` when a sub-step fails but the operation should continue. The entity is marked as errored in HelixObs, but the span stays open so subsequent steps can still run and be recorded.

```python
with tel.operate("write-header", entity_id=event_id) as op:
    try:
        with tel.child_span("header_analysis.dump_header"):
            dump_header(event)
    except Exception as e:
        log.error(f"dump_header failed: {e}")
        op.add_error({"stage": "dump_header", "message": str(e)})
        # operation continues — downstream steps still run and are recorded
    
    try:
        with tel.child_span("header_analysis.write_metadata"):
            write_metadata(event)
    except Exception as e:
        log.error(f"write_metadata failed: {e}")
        op.add_error({"stage": "write_metadata", "message": str(e)})
```

Use `error()` (which ends the span) only when the failure is fatal and no further work should be recorded:

```python
with tel.operate("archival", entity_id=result_id) as op:
    if not disk_available():
        op.error({"message": "no_disk_space"})  # ends span immediately
        return
    write_archive(result_id)
```

### Multi-stage pipeline

Each stage creates its own entity. Parent IDs wire the full provenance DAG.

```python
# Stage 1: ingest raw observation
with tel.create("ingestor", id=frame_id) as token:
    ingest()
    log.info("frame ingested")

# Stage 2: process (derived from the frame)
with tel.create("processor", id=product_id, parents=[frame_id]) as token:
    result = process()
    log.info("product processed")

# Stage 3: aggregate (derived from multiple products)
with tel.create("aggregator", id=result_id, parents=product_ids) as token:
    final = aggregate()
    log.info("result aggregated")
```

---

## 6. Structured Logging

`configure_logging()` installs a JSON formatter on the root logger. Every `logging` call anywhere in the process emits a JSON object while a span is active.

### Process naming convention

Set `process_name` in `setup()` to identify your pipeline process in the **Pipeline Process Logs** Grafana dashboard. The value becomes the `helix_process_name` Loki stream label.

Use a slash-delimited hierarchy:

```
{InstrumentID}/{pipeline}/{stage}
```

Examples:

| Process | `process_name` |
|---|---|
| CHIME L1 beam search | `CHIME/l1-search` |
| CHIME L2 clustering | `CHIME/l2-clustering` |
| CHIME L4 pipeline (top level) | `CHIME/l4-pipeline` |
| CHIME L4 writer subprocess | `CHIME/l4-pipeline/writer` |
| CHIME L4 RFI excision stage | `CHIME/l4-pipeline/rfi-excision` |

The hierarchy serves two purposes:

1. **No name clashes** — the instrument ID prefix namespaces each team's processes.
2. **Regex group filtering** — Loki's regex matcher lets you query an entire subtree. To fetch all L4 logs across every stage:

```
{helix_process_name=~"CHIME/l4-pipeline/.*"}
```

Or all CHIME logs regardless of stage:

```
{helix_process_name=~"CHIME/.*"}
```

**Log while the span is active** to capture entity context:

```python
# CORRECT — log before complete(); active span provides entity context
token = tel.create("processor", id=product_id, parents=[frame_id]).start()
result = process()
log.info("product processed")   # helix_entity_id present
token.complete()

# WRONG — span has ended; no active span context
token = tel.create("processor", id=product_id, parents=[frame_id]).start()
result = process()
token.complete()
log.info("product processed")   # helix_entity_id missing
```

With the context manager pattern this is automatic — the span stays active for the entire `with` block.

**Log line fields:**

| Field | Source | Notes |
|---|---|---|
| `ts` | Log record | ISO 8601 timestamp |
| `level` | Log record | `info`, `warning`, `error`, `debug` |
| `logger` | Log record | Logger name |
| `msg` | Log record | Log message |
| `src` | File + line | Repo-relative path, or GitHub permalink if env vars set |
| `helix_entity_id` | Active OTel span | Omitted if no active span |
| `helix_instrument_id` | Active OTel span | Omitted if no active span |
| `otel_trace_id` | Active OTel span | 32-hex trace ID |
| `otel_span_id` | Active OTel span | 16-hex span ID |
| `exc` | Exception info | Only present when `log.exception()` / `exc_info=True` |

> `helix_entity_id` is **not** a Loki stream label — one stream per entity would exhaust Loki's stream limit. Query entity logs with `| json | helix_entity_id = "..."` at query time.

**Sidecar mode log collector (Grafana Alloy reference):**

```alloy
loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.docker.containers.targets
  forward_to = [loki.process.helix_json.receiver]
}

loki.process "helix_json" {
  stage.json { expressions = { helix_instrument_id = "", level = "" } }
  stage.labels { values = { helix_instrument_id = "", level = "" } }
  forward_to = [loki.write.default.receiver]
}

loki.write "default" { endpoint { url = "http://loki:3100/loki/api/v1/push" } }
```

**Subprocess output is not captured automatically.**

`os.system()` and similar calls fork a child process that writes directly to the terminal's stdout/stderr, bypassing Python's `logging` entirely. That output will not appear in Loki or carry any helix context fields.

Use `subprocess.run()` with `capture_output=True` and log the result explicitly:

```python
import subprocess

result = subprocess.run(cmd, capture_output=True, text=True)
if result.stdout:
    log.info(result.stdout.strip())
if result.stderr:
    log.error(result.stderr.strip())
if result.returncode != 0:
    log.error(f"command failed with exit code {result.returncode}: {cmd}")
```

Any collector that reads stdout and forwards to Loki works (Promtail, Fluent Bit, Vector). The contract is newline-delimited JSON with `helix_instrument_id` and `level` as top-level fields.

---

## 7. Data Model

### entities

| Column | Type | Description |
|---|---|---|
| `id` | TEXT | Entity ID |
| `instrument_id` | TEXT | Instrument identifier |
| `trace_id` | TEXT | OTel trace ID linking to Tempo |
| `timestamp_ns` | BIGINT | Span start time in nanoseconds |
| `parent_ids` | TEXT[] | Parent entity IDs (provenance DAG edges) |
| `metadata` | JSONB | All `helix.*` span attributes |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time (hypertable partition key) |

### entity_events

One row per `helix.*` span event, from any trace associated with the entity.

| Column | Type | Description |
|---|---|---|
| `entity_id` | TEXT | References `entities.id` |
| `instrument_id` | TEXT | Instrument identifier |
| `event_name` | TEXT | e.g. `helix.error`, `helix.event.detection_confirmed` |
| `timestamp_ns` | BIGINT | When the event occurred |
| `trace_id` | TEXT | OTel trace ID of the span that emitted the event |
| `metadata` | JSONB | Attributes from `token.add_event(name, attributes)` |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time |

### entity_operations

One row per `tel.operate()` call.

| Column | Type | Description |
|---|---|---|
| `entity_id` | TEXT | The entity being operated on |
| `instrument_id` | TEXT | Instrument identifier |
| `operation` | TEXT | Operation name (e.g. `archival`, `catalog-registration`, `replication`) |
| `trace_id` | TEXT | OTel trace ID for this operation — links to Tempo |
| `timestamp_ns` | BIGINT | Operation start time in nanoseconds |
| `metadata` | JSONB | Span attributes set inside the `operate()` block |
| `created_at` | TIMESTAMPTZ | Wall-clock insert time |

An entity can have any number of operation rows. Data is retained for 90 days and auto-compressed after 7 days (TimescaleDB policies).

---

## 8. Complete Example

```python
"""
my_pipeline.py — HelixObs integration example.

Environment:
    GATEWAY_ENDPOINT             gRPC address of the HelixObs gateway (default: localhost:4317)
    OTEL_EXPORTER_OTLP_ENDPOINT  gRPC address of the OTel Collector for log shipping
                                 (default: http://localhost:4319)
"""

import logging
import os
import uuid

from helixobs import setup

# ── Startup ───────────────────────────────────────────────────────────────────

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("my.pipeline")

tel = setup(
    "my-instrument.pipeline",
    instrument_id="MY_INSTRUMENT",
    endpoint=os.environ.get("GATEWAY_ENDPOINT", "localhost:4317"),
    otlp=True,
)

# ── Stage 1: ingest a raw observation ────────────────────────────────────────

def ingest_frame() -> str:
    frame_id = f"frame-{uuid.uuid4().hex[:12]}"
    with tel.create("ingestor", id=frame_id) as token:
        data = read_sensor()
        log.info("frame ingested")
        token.add_event("helix.event.frame_received", {"n_samples": len(data)})
    return frame_id


# ── Stage 2: process (derived from the frame) ─────────────────────────────────

def process_item(frame_id: str) -> str | None:
    product_id = f"product-{uuid.uuid4().hex[:12]}"
    token = tel.create("processor", id=product_id, parents=[frame_id]).start()

    try:
        with tel.child_span("quality-filter") as span:
            n_flagged = run_quality_filter()
            span.set_attribute("myinstrument.flagged_channels", n_flagged)

        result = run_detector()
        if result.score < 0.5:
            log.warning("item rejected: score below threshold")
            token.error(metadata={"message": "score_below_threshold", "score": result.score})
            return None

        log.info("item processed")
        token.complete(metadata={"score": result.score})
        return product_id

    except Exception as exc:
        log.exception("processing failed")
        token.error(metadata={"message": str(exc)})
        return None


# ── Stage 3: aggregate N products into one result (N-to-1 provenance) ─────────

def aggregate(product_ids: list[str]) -> str | None:
    if len(product_ids) < 2:
        return None

    result_id = f"result-{uuid.uuid4().hex[:12]}"
    with tel.create("aggregator", id=result_id, parents=product_ids) as token:
        result = run_aggregation(product_ids)
        log.info("result aggregated")
        token.add_event("helix.event.detection_confirmed", {
            "score":          result.score,
            "classification": result.classification,
        })

    return result_id


# ── Post-creation operations ───────────────────────────────────────────────────

def archive_and_distribute(result_id: str) -> None:
    with tel.operate("archival", entity_id=result_id) as op:
        size_mb = write_archive(result_id)
        op.set_attribute("myinstrument.archive_size_mb", size_mb)
        log.info(f"archive written: {size_mb:.1f} MB")

    with tel.operate("catalog-registration", entity_id=result_id) as op:
        register(result_id)
        log.info("result registered in catalog")

    for site in ["site-a", "site-b"]:
        with tel.operate("replication", entity_id=result_id) as op:
            op.set_attribute("myinstrument.replication_dest", site)
            if not replicate_to(site):
                log.error(f"replication to {site} timed out")
                op.error({"message": "replication_timeout", "dest": site})
```

---

### Grafana dashboards

| Dashboard | URL | Description |
|---|---|---|
| **Entity Inspector** | `http://localhost:3001/d/helix-entity-inspector` | Provenance DAG, metadata, events, span tree, and correlated logs for any entity ID |
| **Error Entities** | `http://localhost:3001/d/helix-error-entities` | Table of all failed entities with error rate chart |
| **Platform Health** | `http://localhost:3001/d/helix-platform-health` | Gateway throughput, DB write latency, trace store, backend health |
