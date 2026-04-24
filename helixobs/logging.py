"""
helixobs.logging
────────────────
Structured log context injection for HelixObs instrument pipelines.

``configure_logging()`` supports two shipping modes:

**stdout mode** (default, ``otlp=False``):
  Installs a JSON formatter so every log line is a single-line JSON object.
  Grafana Alloy (or any log collector on the host) tails stdout and pushes
  to Loki.  No extra dependencies required.

**OTLP mode** (``otlp=True``):
  Ships logs directly to the OTel Collector over gRPC, exactly like traces.
  Requires ``opentelemetry-exporter-otlp-proto-grpc``.  Use this for
  greenfield services that have no sidecar.

In both modes the log-record factory injects OTel span context fields:
  - helixEntityID, helixInstrumentID  (from active span attributes)
  - otelTraceID, otelSpanID
  - helixSource  (repo-relative file:line, or GitHub permalink)

When the environment variables ``GITHUB_REPO`` and ``GIT_COMMIT_SHA`` are
set the ``helixSource`` field becomes a full GitHub permalink.

Usage (once at application startup):

    from helixobs.logging import configure_logging

    # Greenfield service — send logs via OTLP to the collector
    configure_logging(otlp=True)

    # Legacy / sidecar service — write JSON to stdout as before
    configure_logging()

The OTLP endpoint is read from ``OTEL_EXPORTER_OTLP_ENDPOINT`` (default
``http://localhost:4317``).
"""

from __future__ import annotations

import json
import logging
import os
import re

from opentelemetry import trace


def configure_logging(*, otlp: bool = False) -> None:
    """Install the helix factory and attach log handlers on the root logger.

    Args:
        otlp: When True, ship logs via OTLP gRPC instead of stdout JSON.
              Requires ``opentelemetry-exporter-otlp-proto-grpc``.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    _install_factory()
    if otlp:
        _install_otlp_handler()
    else:
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


_otlp_handler_installed = False


def _install_otlp_handler() -> None:
    global _otlp_handler_installed
    if _otlp_handler_installed:
        return
    _otlp_handler_installed = True

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError as exc:
        raise ImportError(
            "OTLP log shipping requires 'opentelemetry-exporter-otlp-proto-grpc'. "
            "Install it with: pip install opentelemetry-exporter-otlp-proto-grpc"
        ) from exc

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    resource = Resource.create()

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
    )
    set_logger_provider(provider)

    handler = LoggingHandler(logger_provider=provider)
    logging.getLogger().addHandler(handler)


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

_installed = False


def _install_factory() -> None:
    global _installed
    if _installed:
        return
    _installed = True

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
            record.helixEntityID     = (attrs.get("helix.entity.id")
                                        or attrs.get("helix.log.entity_id", ""))
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

        record.msg = f"{record.msg}  src={record.helixSource}"
        record.args = None

        return record

    logging.setLogRecordFactory(_factory)
