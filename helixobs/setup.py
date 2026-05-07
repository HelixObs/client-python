"""
helixobs.setup
──────────────
Convenience entry point that wires traces and logs in one call.
"""

from __future__ import annotations

from .instrument import Instrument
from .logging import configure_logging


def setup(
    service_name: str,
    *,
    instrument_id: str,
    endpoint: str = "localhost:4317",
    insecure: bool = True,
    otlp: bool = False,
    log_endpoint: str | None = None,
) -> Instrument:
    """Configure logging and return a ready-to-use Instrument.

    This is the recommended entry point for most pipelines. It ensures
    logs and traces share the same ``service_name`` without any duplication.

    Parameters
    ----------
    service_name:
        OTel service name — identifies this pipeline in Tempo and Loki.
        e.g. ``"chime.l1"``
    instrument_id:
        Instrument identifier stamped on every entity span. e.g. ``"CHIME"``
    endpoint:
        OTLP gRPC endpoint of the HelixObs gateway for traces.
        Default: ``"localhost:4317"``
    insecure:
        Use plaintext gRPC. Default: ``True``
    otlp:
        When True, ship logs via OTLP gRPC to the OTel Collector.
        When False (default), write JSON to stdout for sidecar collection.
    log_endpoint:
        OTLP gRPC endpoint for log shipping. Only used when ``otlp=True``.
        Defaults to the value of ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var,
        or ``"http://localhost:4319"`` if unset.

    Returns
    -------
    Instrument
        Ready to use for ``tel.create()`` and ``tel.operate()``.

    Example
    -------
    ::

        from helixobs import setup
        import logging

        log = logging.getLogger(__name__)
        tel = setup("chime.l1", instrument_id="CHIME", endpoint="gateway:4317", otlp=True)

        with tel.create("l1-search", id=beam_id) as token:
            log.info("beam processed")   # helix_entity_id present
    """
    import os

    if otlp and log_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", log_endpoint)

    configure_logging(otlp=otlp, service_name=service_name if otlp else None)

    return Instrument(
        service_name=service_name,
        instrument_id=instrument_id,
        endpoint=endpoint,
        insecure=insecure,
    )
