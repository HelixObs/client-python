"""
helixobs.logging
────────────────
Structured log context injection for HelixObs instrument pipelines.

``configure_logging()`` does two things:

1. Installs a log-record factory that reads the active OTel span and
   injects helix context fields into every record:
     - helixEntityID, helixInstrumentID  (from span attributes)
     - otelTraceID, otelSpanID
     - helixSource  (repo-relative file:line, or GitHub permalink)

2. Attaches a JSON formatter to the root handler so every log line is a
   single-line JSON object.  Grafana Alloy (or any log collector) can
   then parse the JSON and promote ``helix_entity_id`` / ``helix_instrument_id``
   as Loki stream labels without the application making any HTTP calls.

When the environment variables ``GITHUB_REPO`` and ``GIT_COMMIT_SHA`` are
set the ``helixSource`` field becomes a full GitHub permalink.

Usage (once at application startup):

    from helixobs.logging import configure_logging

    configure_logging()
"""

from __future__ import annotations

import json
import logging
import os
import re

from opentelemetry import trace


def configure_logging() -> None:
    """Install the helix factory and JSON formatter on the root logger.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    _install_factory()
    _install_json_handler()


# ── JSON formatter ────────────────────────────────────────────────────────────

class _HelixJsonFormatter(logging.Formatter):
    """Emit one JSON object per log record with all helix context fields."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj = {
            "ts":                    self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":                 record.levelname.lower(),
            "logger":                record.name,
            "msg":                   record.message,
            "src":                   getattr(record, "helixSource", ""),
            "helix_entity_id":       getattr(record, "helixEntityID", ""),
            "helix_instrument_id":   getattr(record, "helixInstrumentID", ""),
            "otel_trace_id":         getattr(record, "otelTraceID", ""),
            "otel_span_id":          getattr(record, "otelSpanID", ""),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Drop keys with empty values to keep lines short
        obj = {k: v for k, v in obj.items() if v}
        return json.dumps(obj)


_json_handler_installed = False


def _install_json_handler() -> None:
    global _json_handler_installed
    if _json_handler_installed:
        return
    _json_handler_installed = True

    root = logging.getLogger()
    # Replace any existing handlers' formatters; add a stderr handler if none.
    if root.handlers:
        for h in root.handlers:
            h.setFormatter(_HelixJsonFormatter())
    else:
        h = logging.StreamHandler()
        h.setFormatter(_HelixJsonFormatter())
        root.addHandler(h)


# ── Source-location helpers ───────────────────────────────────────────────────

_SITE_PKGS_RE = re.compile(r".*[/\\]site-packages[/\\]")


def _normalize_path(pathname: str, filename: str) -> str:
    m = _SITE_PKGS_RE.search(pathname)
    if m:
        return pathname[m.end():]
    try:
        return os.path.relpath(pathname)
    except ValueError:
        return filename


# ── Factory ───────────────────────────────────────────────────────────────────

_factory_installed = False


def _install_factory() -> None:
    global _factory_installed
    if _factory_installed:
        return
    _factory_installed = True

    github_repo    = os.environ.get("GITHUB_REPO", "").rstrip("/")
    git_ref        = os.environ.get("GIT_COMMIT_SHA") or os.environ.get("GIT_BRANCH", "main")

    _prev = logging.getLogRecordFactory()

    def _factory(name, level, fn, lno, msg, args, exc_info,
                 func=None, sinfo=None):
        record = _prev(name, level, fn, lno, msg, args, exc_info, func, sinfo)

        span = trace.get_current_span()
        sc   = span.get_span_context()
        if sc.is_valid:
            record.otelTraceID       = format(sc.trace_id, "032x")
            record.otelSpanID        = format(sc.span_id,  "016x")
            attrs                    = getattr(span, "attributes", {}) or {}
            record.helixEntityID     = attrs.get("helix.entity.id", "")
            record.helixInstrumentID = attrs.get("helix.instrument.id", "")
        else:
            record.otelTraceID       = ""
            record.otelSpanID        = ""
            record.helixEntityID     = ""
            record.helixInstrumentID = ""

        rel = _normalize_path(record.pathname, record.filename)
        if github_repo:
            record.helixSource = f"{github_repo}/blob/{git_ref}/{rel}#L{record.lineno}"
        else:
            record.helixSource = f"{rel}#L{record.lineno}"

        return record

    logging.setLogRecordFactory(_factory)
