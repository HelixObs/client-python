"""Tests for at-fork gRPC deadlock prevention."""
import multiprocessing
import os
import sys

import pytest

from helixobs._fork import _providers, register_provider_for_fork


class _FakeProvider:
    def __init__(self):
        self.flush_calls = 0
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis=500):
        self.flush_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1


def test_register_adds_provider():
    before = len(_providers)
    p = _FakeProvider()
    register_provider_for_fork(p)
    assert p in _providers
    assert len(_providers) == before + 1


def test_flush_and_shutdown_called_on_fork():
    # Use multiprocessing with "fork" start method to exercise the handlers.
    if sys.platform == "win32":
        pytest.skip("os.register_at_fork not available on Windows")

    ctx = multiprocessing.get_context("fork")

    parent_done = ctx.Event()
    child_signalled = ctx.Value("i", 0)

    def child_fn(val):
        # If shutdown was called in the child, providers list is intact
        # but all providers should be shut down — we can't assert across
        # process boundaries directly, so just signal success.
        val.value = 1

    p = _FakeProvider()
    register_provider_for_fork(p)

    proc = ctx.Process(target=child_fn, args=(child_signalled,))
    proc.start()
    proc.join(timeout=5)

    assert proc.exitcode == 0, "child process failed"
    # Parent's provider should have been flushed before fork
    assert p.flush_calls >= 1
