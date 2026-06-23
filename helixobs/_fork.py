"""At-fork handlers to prevent gRPC deadlock in child processes.

When a process with active gRPC background threads (BatchSpanProcessor,
BatchLogRecordProcessor) calls os.fork(), the child inherits locked mutexes
from the parent's threads — which are dead in the child — causing a permanent
deadlock on the next gRPC call.

This module registers os.register_at_fork() handlers that:

  before:         force-flush all pending exports (drain queues, release locks)
  after_in_child: shut down all providers (stop background threads in child)

The parent continues exporting telemetry normally. The child's OTel state is
cleanly stopped so its own work (subprocess replication, CADC upload, etc.)
is never blocked waiting for a mutex that will never be released.
"""

from __future__ import annotations

import os

_providers: list = []
_registered = False


def register_provider_for_fork(provider: object) -> None:
    """Register a TracerProvider or LoggerProvider for at-fork handling.

    Safe to call multiple times with different providers — handlers are only
    registered with os.register_at_fork once.
    """
    global _registered
    _providers.append(provider)
    if _registered:
        return
    _registered = True
    try:
        os.register_at_fork(before=_flush_all, after_in_child=_shutdown_all)
    except AttributeError:
        pass  # os.register_at_fork not available on Windows


def _flush_all() -> None:
    """Force-flush all providers before fork (called in parent)."""
    for p in _providers:
        try:
            p.force_flush(timeout_millis=500)
        except Exception:
            pass


def _shutdown_all() -> None:
    """Shut down all providers after fork (called in child only)."""
    for p in _providers:
        try:
            p.shutdown()
        except Exception:
            pass
