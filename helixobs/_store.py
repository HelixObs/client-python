"""
In-process trace store: entity_id → OTel SpanContext.

Used to resolve parent entity IDs into OTel span links when a child entity
is created in the same process.  For cross-process parents, the gateway
resolves them server-side using the helix.parent.ids span attribute.
"""

from __future__ import annotations

import threading
from opentelemetry.trace import SpanContext


class TraceStore:
    """Thread-safe bounded FIFO map: entity_id → SpanContext."""

    def __init__(self, maxsize: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, SpanContext] = {}
        self._maxsize = maxsize

    def put(self, entity_id: str, ctx: SpanContext) -> None:
        with self._lock:
            if len(self._store) >= self._maxsize:
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[entity_id] = ctx

    def get(self, entity_id: str) -> SpanContext | None:
        with self._lock:
            return self._store.get(entity_id)
