"""
helixobs.setup
──────────────
Convenience entry point that wires traces and logs in one call.
"""

from __future__ import annotations

import inspect
from typing import Type, TypeVar

from .instrument import Instrument
from .logging import configure_logging

T = TypeVar("T", bound=Instrument)


def setup(
    service_name: str,
    *,
    instrument_id: str | None = None,
    endpoint: str = "localhost:4317",
    insecure: bool = True,
    otlp: bool = False,
    log_endpoint: str | None = None,
    process_name: str | None = None,
    instrument_class: Type[T] = Instrument,
) -> T:
    """Configure logging and return a ready-to-use Instrument.

    This is the recommended entry point for most pipelines. It ensures
    logs and traces share the same ``service_name`` without any duplication.

    Parameters
    ----------
    service_name:
        OTel service name — identifies this pipeline in Tempo and Loki.
        e.g. ``"chime.l1"``
    instrument_id:
        Instrument identifier stamped on every entity span. e.g. ``"CHIME"``.
        Required when using the base ``Instrument`` class. Omit when using a
        domain subclass that hard-codes its own ID (e.g. ``CHIMEInstrument``).
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
    instrument_class:
        Instrument subclass to instantiate. Use when your domain subclasses
        ``Instrument`` (e.g. ``CHIMEInstrument``). Default: ``Instrument``.

    Returns
    -------
    Instrument (or subclass)
        Ready to use for ``tel.create()`` and ``tel.operate()``.

    Example
    -------
    ::

        from helixobs import setup

        # Base Instrument — instrument_id required
        tel = setup("my.pipeline", instrument_id="MY_INST", endpoint="gateway:4317", otlp=True)

        # Domain subclass — instrument_id owned by the class, not the caller
        from chime import CHIMEInstrument
        tel = setup("chime.simulator", endpoint="gateway:4317",
                    otlp=True, instrument_class=CHIMEInstrument)
    """
    import os

    if otlp and log_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", log_endpoint)

    configure_logging(otlp=otlp, service_name=service_name if otlp else None)

    sig = inspect.signature(instrument_class.__init__)
    kwargs: dict = dict(service_name=service_name, endpoint=endpoint, insecure=insecure)
    if "instrument_id" in sig.parameters:
        if instrument_id is None:
            raise ValueError(
                "instrument_id is required when using the base Instrument class. "
                "Pass instrument_id=... to setup()."
            )
        kwargs["instrument_id"] = instrument_id
    if "process_name" in sig.parameters and process_name is not None:
        kwargs["process_name"] = process_name

    return instrument_class(**kwargs)
