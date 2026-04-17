"""
helixobs.logging
────────────────
Log context injection — injects helix.entity.id and helix.instrument.id
into every log record emitted anywhere in the process, including from
sub-modules and third-party libraries.

Uses a log-record factory (not a filter on the root logger) so that child
loggers propagating up the hierarchy are also covered.  A filter on
logging.getLogger() only fires for records emitted directly to the root
logger, not for child loggers that propagate upward.

Usage (once at application startup, after creating your Instrument):

    from helixobs.logging import configure_logging
    configure_logging()

After this call, every log line emitted while a helix span is active will
carry trace_id, span_id, helix.entity.id, and helix.instrument.id as extra
fields, enabling Grafana's cross-signal navigation between Loki and Tempo.
"""

import logging

from opentelemetry import trace


def configure_logging() -> None:
    """Install the helix log-record factory on the root logger.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    _install_factory()


# ── Implementation ────────────────────────────────────────────────────────────

_installed = False


def _install_factory() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    _prev = logging.getLogRecordFactory()

    def _factory(name, level, fn, lno, msg, args, exc_info,
                 func=None, sinfo=None):
        record = _prev(name, level, fn, lno, msg, args, exc_info, func, sinfo)
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc.is_valid:
            record.otelTraceID   = format(sc.trace_id, "032x")
            record.otelSpanID    = format(sc.span_id,  "016x")
            attrs = getattr(span, "attributes", {}) or {}
            record.helixEntityID     = attrs.get("helix.entity.id", "")
            record.helixInstrumentID = attrs.get("helix.instrument.id", "")
        else:
            record.otelTraceID       = ""
            record.otelSpanID        = ""
            record.helixEntityID     = ""
            record.helixInstrumentID = ""
        return record

    logging.setLogRecordFactory(_factory)
