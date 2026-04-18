"""
helixobs.logging
────────────────
Log context injection and optional Loki shipping.

``configure_logging()`` installs a log-record factory that:
  - injects helix context fields (otelTraceID, otelSpanID, helixEntityID,
    helixInstrumentID) into every log record
  - appends a ``src=`` source-location tag to every log message so the
    originating file and line are visible in Loki and stdout

When the environment variables ``GITHUB_REPO`` and ``GIT_COMMIT_SHA`` are
set the ``src=`` tag becomes a full GitHub permalink, e.g.:
    src=https://github.com/HelixObs/client-python/blob/abc123/helixobs/instrument.py#L42

Otherwise it falls back to a repo-relative path:
    src=helixobs/instrument.py#L42

``configure_loki()`` attaches a non-blocking handler that ships records to
Grafana Loki.  Call *after* ``configure_logging()`` so that helix fields are
already present on the records.

Usage (once at application startup):

    from helixobs.logging import configure_logging, configure_loki

    configure_logging()
    configure_loki(url="http://loki:3100", labels={"app": "chime"})
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from helixobs.loki import LokiHandler


def configure_logging() -> None:
    """Install the helix log-record factory on the root logger.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    _install_factory()


def configure_loki(
    url: str,
    labels: dict[str, str] | None = None,
    level: int = logging.NOTSET,
    timeout: float = 5.0,
    logger: logging.Logger | None = None,
) -> "LokiHandler":
    """Attach a non-blocking Loki handler to *logger* (default: root logger).

    Args:
        url:     Loki base URL, e.g. ``http://loki:3100``.
        labels:  Static stream labels added to every log line.
        level:   Minimum log level forwarded to Loki (default: all levels).
        timeout: HTTP request timeout for each push, in seconds.
        logger:  Logger to attach to; defaults to the root logger.

    Returns:
        The :class:`~helixobs.loki.LokiHandler` instance, so callers can
        ``handler.close()`` during a clean shutdown or change the formatter.
    """
    from helixobs.loki import LokiHandler

    handler = LokiHandler(url=url, labels=labels, timeout=timeout, level=level)
    target = logger if logger is not None else logging.getLogger()
    target.addHandler(handler)
    return handler


# ── Source-location helpers ───────────────────────────────────────────────────
#
# WHY we modify record.msg rather than using a filter or formatter:
#   A filter on the root logger does NOT fire for child loggers that propagate
#   upward (Python calls each handler directly, bypassing parent filters).
#   A formatter only runs inside a handler, so it can't be shared across all
#   handlers transparently.  Modifying the record in the factory means the
#   source tag is present everywhere — stdout, files, and Loki — without any
#   per-handler configuration.

_SITE_PKGS_RE = re.compile(r".*[/\\]site-packages[/\\]")


def _normalize_path(pathname: str, filename: str) -> str:
    """Return a short, human-readable path for the source location tag.

    Priority:
    1. Strip the site-packages prefix (installed package path).
    2. Make relative to the current working directory.
    3. Fall back to the basename (cross-drive on Windows, or other edge cases).
    """
    m = _SITE_PKGS_RE.search(pathname)
    if m:
        return pathname[m.end():]
    try:
        return os.path.relpath(pathname)
    except ValueError:
        return filename


# ── Implementation ────────────────────────────────────────────────────────────

_installed = False


def _install_factory() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    github_repo    = os.environ.get("GITHUB_REPO", "").rstrip("/")
    git_commit_sha = os.environ.get("GIT_COMMIT_SHA", "")

    _prev = logging.getLogRecordFactory()

    def _factory(name, level, fn, lno, msg, args, exc_info,
                 func=None, sinfo=None):
        record = _prev(name, level, fn, lno, msg, args, exc_info, func, sinfo)

        # ── OTel / helix context ──────────────────────────────────────────
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc.is_valid:
            record.otelTraceID       = format(sc.trace_id, "032x")
            record.otelSpanID        = format(sc.span_id,  "016x")
            attrs = getattr(span, "attributes", {}) or {}
            record.helixEntityID     = attrs.get("helix.entity.id", "")
            record.helixInstrumentID = attrs.get("helix.instrument.id", "")
        else:
            record.otelTraceID       = ""
            record.otelSpanID        = ""
            record.helixEntityID     = ""
            record.helixInstrumentID = ""

        # ── Source location ───────────────────────────────────────────────
        rel = _normalize_path(record.pathname, record.filename)
        if github_repo and git_commit_sha:
            src = f"{github_repo}/blob/{git_commit_sha}/{rel}#L{record.lineno}"
        else:
            src = f"{rel}#L{record.lineno}"
        record.helixSource = src

        try:
            body = record.getMessage()
        except Exception:
            body = str(record.msg)
        record.msg  = f"{body}  src={src}"
        record.args = None

        return record

    logging.setLogRecordFactory(_factory)
