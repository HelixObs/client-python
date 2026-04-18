"""
End-to-end tests for the helixobs Python client.

Requires the stack in tests/e2e/docker-compose.yml to be running.
Run via the e2e CI workflow or locally:

    docker build -t helixobs-gateway ../../../gateway
    GATEWAY_MIGRATIONS_DIR=../../../gateway/migrations \\
        docker compose -f tests/e2e/docker-compose.yml up -d --wait
    pytest -v -m e2e tests/e2e/
    docker compose -f tests/e2e/docker-compose.yml down -v
"""

import os
import time
import urllib.request
import uuid

import psycopg2
import pytest

from helixobs.instrument import Instrument

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_ENDPOINT = os.environ.get("GATEWAY_ENDPOINT", "localhost:4317")
METRICS_URL      = os.environ.get("METRICS_URL",      "http://localhost:2112/metrics")
DB_DSN           = os.environ.get("TEST_DB_URL",
                                  "host=localhost port=5432 dbname=helixobs "
                                  "user=helix password=helix sslmode=disable")

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def wait_for_gateway():
    """Block until the gateway metrics endpoint responds."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(METRICS_URL, timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    pytest.fail("Gateway did not become ready within 30 s")


@pytest.fixture(scope="session")
def db():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def instrument():
    """A real Instrument connected to the e2e gateway. Flushes on teardown."""
    inst = Instrument(
        service_name="e2e-test",
        instrument_id="E2E",
        endpoint=GATEWAY_ENDPOINT,
        insecure=True,
    )
    yield inst
    inst._provider.force_flush(timeout_millis=5_000)
    inst._provider.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────

def poll(condition, timeout: float = 10.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def entity_in_db(db, entity_id: str) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM entities WHERE id = %s", (entity_id,))
        return cur.fetchone() is not None


def scrape_total(metric_name: str) -> float:
    with urllib.request.urlopen(METRICS_URL, timeout=5) as resp:
        body = resp.read().decode()
    total = 0.0
    for line in body.splitlines():
        if line.startswith(metric_name + "{") or line.startswith(metric_name + " "):
            try:
                total += float(line.rsplit(" ", 1)[-1])
            except ValueError:
                pass
    return total


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_entity_appears_in_db(instrument, db):
    entity_id = f"e2e-{uuid.uuid4().hex[:12]}"
    token = instrument.track("stage", id=entity_id)
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5_000)

    assert poll(lambda: entity_in_db(db, entity_id)), \
        f"entity {entity_id!r} never appeared in TimescaleDB"


@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_parent_ids_stored(instrument, db):
    parent_id = f"e2e-parent-{uuid.uuid4().hex[:12]}"
    child_id  = f"e2e-child-{uuid.uuid4().hex[:12]}"

    pt = instrument.track("stage-a", id=parent_id)
    instrument.complete(pt)
    ct = instrument.track("stage-b", id=child_id, parents=[parent_id])
    instrument.complete(ct)
    instrument._provider.force_flush(timeout_millis=5_000)

    def child_has_parent():
        with db.cursor() as cur:
            cur.execute("SELECT parent_ids FROM entities WHERE id = %s", (child_id,))
            row = cur.fetchone()
            return row is not None and parent_id in row[0]

    assert poll(child_has_parent), \
        f"parent_id {parent_id!r} not stored on child {child_id!r}"


@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_entities_total_counter_increments(instrument, db):
    before = scrape_total("helix_entities_total")
    entity_id = f"e2e-counter-{uuid.uuid4().hex[:12]}"
    token = instrument.track("stage", id=entity_id)
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5_000)

    assert poll(lambda: scrape_total("helix_entities_total") > before), \
        "helix_entities_total did not increment"


@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_error_event_recorded(instrument, db):
    entity_id = f"e2e-err-{uuid.uuid4().hex[:12]}"
    token = instrument.track("stage", id=entity_id)
    instrument.error(token, {"message": "something went wrong"})
    instrument._provider.force_flush(timeout_millis=5_000)

    assert poll(lambda: entity_in_db(db, entity_id)), \
        f"errored entity {entity_id!r} never appeared in TimescaleDB"

    def event_recorded():
        with db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM entity_events WHERE entity_id = %s AND event_name = 'helix.error'",
                (entity_id,),
            )
            return cur.fetchone() is not None

    assert poll(event_recorded), \
        f"helix.error event not found for entity {entity_id!r}"


@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_n_to_1_provenance_chain(instrument, db):
    """Multiple candidates → one event: the N-to-1 provenance pattern."""
    block_id = f"e2e-block-{uuid.uuid4().hex[:12]}"
    cand_ids = [f"e2e-cand-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    event_id = f"e2e-event-{uuid.uuid4().hex[:12]}"

    bt = instrument.track("x-engine", id=block_id)
    instrument.complete(bt)

    for cid in cand_ids:
        ct = instrument.track("classifier", id=cid, parents=[block_id])
        instrument.complete(ct)

    et = instrument.track("clustering", id=event_id, parents=cand_ids)
    instrument.complete(et)
    instrument._provider.force_flush(timeout_millis=5_000)

    def event_has_all_parents():
        with db.cursor() as cur:
            cur.execute("SELECT parent_ids FROM entities WHERE id = %s", (event_id,))
            row = cur.fetchone()
            if row is None:
                return False
            stored = row[0]
            return all(cid in stored for cid in cand_ids)

    assert poll(event_has_all_parents), \
        f"event {event_id!r} did not have all 3 candidate parents in DB"
