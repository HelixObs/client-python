"""
helixobs.chime
──────────────
CHIME-specific Instrument subclass.

Pre-configures instrument_id="CHIME" and exposes helpers for the CHIME
semantic conventions defined in the HelixObs design document.

Usage:

    from helixobs.chime import CHIMEInstrument

    tel = CHIMEInstrument(
        service_name="chime.frb-classifier",
        endpoint="interceptor.local:4317",
    )

    with tel.stage("frb_classifier", id=cand_id, parents=[block_id]) as span:
        result = classify(candidate)
        if result.rfi_probability > 0.9:
            span.add_event("helix.event.rfi_flagged", {
                "helix.chime.rfi_fraction": result.rfi_probability,
                "helix.chime.algorithm":    "spectral_kurtosis",
            })
        elif result.confidence > 0.95:
            span.add_event("helix.event.candidate_promoted", {
                "helix.chime.snr":        result.snr,
                "helix.chime.dm":         result.dm,
                "helix.chime.confidence": result.confidence,
            })
"""

from ..instrument import Instrument, Token

INSTRUMENT_ID = "CHIME"

# ── Attribute key constants (CHIME semantic conventions) ──────────────────────

# Data block attributes
ATTR_FREQ_MIN_MHZ     = "helix.chime.freq_min_mhz"
ATTR_FREQ_MAX_MHZ     = "helix.chime.freq_max_mhz"
ATTR_N_FREQ_CHANNELS  = "helix.chime.n_freq_channels"
ATTR_DURATION_S       = "helix.chime.duration_s"
ATTR_FPGA_RACK        = "helix.chime.fpga_rack"

# Candidate attributes
ATTR_BEAM_ID          = "helix.chime.beam_id"
ATTR_DM               = "helix.chime.dm"
ATTR_SNR              = "helix.chime.snr"
ATTR_ARRIVAL_DELAY_NS = "helix.chime.arrival_delay_ns"

# Event attributes
ATTR_RA             = "helix.chime.ra"
ATTR_DEC            = "helix.chime.dec"
ATTR_CLASSIFICATION = "helix.chime.classification"


class CHIMEInstrument(Instrument):
    """Instrument pre-configured for CHIME with semantic convention helpers."""

    def __init__(
        self,
        service_name: str,
        endpoint: str = "localhost:4317",
        insecure: bool = True,
    ) -> None:
        super().__init__(
            service_name=service_name,
            instrument_id=INSTRUMENT_ID,
            endpoint=endpoint,
            insecure=insecure,
        )

    # ── Entity helpers ────────────────────────────────────────────────

    def data_block_metadata(
        self,
        token: Token,
        *,
        fpga_rack: str,
        freq_min_mhz: float = 400.0,
        freq_max_mhz: float = 800.0,
        n_freq_channels: int = 1024,
        duration_s: float = 8.0,
    ) -> None:
        """Stamp CHIME data-block semantic convention attributes onto a span."""
        span = token._span
        span.set_attribute(ATTR_FPGA_RACK,       fpga_rack)
        span.set_attribute(ATTR_FREQ_MIN_MHZ,    freq_min_mhz)
        span.set_attribute(ATTR_FREQ_MAX_MHZ,    freq_max_mhz)
        span.set_attribute(ATTR_N_FREQ_CHANNELS, n_freq_channels)
        span.set_attribute(ATTR_DURATION_S,      duration_s)

    def candidate_metadata(
        self,
        token: Token,
        *,
        beam_id: int,
        dm: float,
        snr: float,
        arrival_delay_ns: int = 0,
    ) -> None:
        """Stamp CHIME candidate semantic convention attributes onto a span."""
        span = token._span
        span.set_attribute(ATTR_BEAM_ID,          beam_id)
        span.set_attribute(ATTR_DM,               dm)
        span.set_attribute(ATTR_SNR,              snr)
        span.set_attribute(ATTR_ARRIVAL_DELAY_NS, arrival_delay_ns)

    def event_metadata(
        self,
        token: Token,
        *,
        ra: float,
        dec: float,
        classification: str,
    ) -> None:
        """Stamp CHIME astrophysical event semantic convention attributes."""
        span = token._span
        span.set_attribute(ATTR_RA,             ra)
        span.set_attribute(ATTR_DEC,            dec)
        span.set_attribute(ATTR_CLASSIFICATION, classification)
