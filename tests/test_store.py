"""Tests for TraceStore — thread-safe bounded entity_id → SpanContext map."""

import threading

from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from helixobs._store import TraceStore


def _ctx(trace_id: int, span_id: int) -> SpanContext:
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def test_put_and_get():
    store = TraceStore()
    ctx = _ctx(1, 1)
    store.put("entity-1", ctx)
    assert store.get("entity-1") == ctx


def test_get_missing_returns_none():
    store = TraceStore()
    assert store.get("nonexistent") is None


def test_overwrite():
    store = TraceStore()
    ctx1 = _ctx(1, 1)
    ctx2 = _ctx(2, 2)
    store.put("entity-1", ctx1)
    store.put("entity-1", ctx2)
    assert store.get("entity-1") == ctx2


def test_maxsize_evicts_oldest():
    store = TraceStore(maxsize=3)
    for i in range(3):
        store.put(f"entity-{i}", _ctx(i, i))
    # Adding a 4th should evict entity-0 (oldest)
    store.put("entity-3", _ctx(3, 3))
    assert store.get("entity-0") is None
    assert store.get("entity-3") is not None


def test_maxsize_retains_most_recent():
    store = TraceStore(maxsize=3)
    for i in range(5):
        store.put(f"entity-{i}", _ctx(i, i))
    # Only the 3 most recent should survive
    assert store.get("entity-2") is not None
    assert store.get("entity-3") is not None
    assert store.get("entity-4") is not None


def test_concurrent_puts_do_not_corrupt():
    store = TraceStore(maxsize=1_000)
    errors = []

    def writer(n: int):
        try:
            for i in range(100):
                store.put(f"entity-{n}-{i}", _ctx(n, i))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


def test_concurrent_puts_and_gets():
    store = TraceStore(maxsize=1_000)
    store.put("shared", _ctx(99, 99))
    errors = []

    def reader():
        try:
            for _ in range(200):
                store.get("shared")
        except Exception as e:
            errors.append(e)

    def writer():
        try:
            for i in range(200):
                store.put(f"new-{i}", _ctx(i, i))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(5)]
    threads += [threading.Thread(target=writer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
