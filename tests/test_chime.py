"""Tests for CHIMEInstrument — semantic convention helpers."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry import trace

from helixobs.chime import (
    CHIMEInstrument,
    ATTR_BEAM_ID,
    ATTR_DM,
    ATTR_SNR,
    ATTR_ARRIVAL_DELAY_NS,
    ATTR_FPGA_RACK,
    ATTR_FREQ_MIN_MHZ,
    ATTR_FREQ_MAX_MHZ,
    ATTR_N_FREQ_CHANNELS,
    ATTR_DURATION_S,
    ATTR_RA,
    ATTR_DEC,
    ATTR_CLASSIFICATION,
    INSTRUMENT_ID,
)
from helixobs._store import TraceStore
from tests.conftest import finished_spans


@pytest.fixture
def chime_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def chime(chime_exporter):
    """CHIMEInstrument wired to an in-memory exporter."""
    tel = CHIMEInstrument.__new__(CHIMEInstrument)
    tel.instrument_id = INSTRUMENT_ID
    tel._store = TraceStore()

    from opentelemetry.sdk.resources import Resource
    resource = Resource.create({"service.name": "test-chime", "helix.instrument.id": INSTRUMENT_ID})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(chime_exporter))
    trace.set_tracer_provider(provider)
    tel._tracer = trace.get_tracer("helixobs", tracer_provider=provider)
    tel._provider = provider
    return tel


class TestCHIMEInstrumentID:
    def test_instrument_id_is_chime(self, chime):
        assert chime.instrument_id == "CHIME"

    def test_span_carries_chime_instrument_id(self, chime, chime_exporter):
        token = chime.track("correlator", id="block-1")
        chime.complete(token)
        span = finished_spans(chime_exporter)[0]
        assert span.attributes["helix.instrument.id"] == "CHIME"


class TestDataBlockMetadata:
    def test_all_attributes_set(self, chime, chime_exporter):
        token = chime.track("x-engine", id="block-1")
        chime.data_block_metadata(
            token,
            fpga_rack="gpu-rack-3",
            freq_min_mhz=400.0,
            freq_max_mhz=800.0,
            n_freq_channels=1024,
            duration_s=8.0,
        )
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_FPGA_RACK] == "gpu-rack-3"
        assert attrs[ATTR_FREQ_MIN_MHZ] == 400.0
        assert attrs[ATTR_FREQ_MAX_MHZ] == 800.0
        assert attrs[ATTR_N_FREQ_CHANNELS] == 1024
        assert attrs[ATTR_DURATION_S] == 8.0

    def test_defaults_applied(self, chime, chime_exporter):
        token = chime.track("x-engine", id="block-2")
        chime.data_block_metadata(token, fpga_rack="rack-1")
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_FREQ_MIN_MHZ] == 400.0
        assert attrs[ATTR_FREQ_MAX_MHZ] == 800.0
        assert attrs[ATTR_N_FREQ_CHANNELS] == 1024
        assert attrs[ATTR_DURATION_S] == 8.0


class TestCandidateMetadata:
    def test_all_attributes_set(self, chime, chime_exporter):
        token = chime.track("frb-classifier", id="cand-1")
        chime.candidate_metadata(token, beam_id=42, dm=341.2, snr=18.3)
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_BEAM_ID] == 42
        assert attrs[ATTR_DM] == 341.2
        assert attrs[ATTR_SNR] == 18.3
        assert attrs[ATTR_ARRIVAL_DELAY_NS] == 0  # default

    def test_arrival_delay_set(self, chime, chime_exporter):
        token = chime.track("frb-classifier", id="cand-2")
        chime.candidate_metadata(token, beam_id=1, dm=100.0, snr=10.0, arrival_delay_ns=12345)
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_ARRIVAL_DELAY_NS] == 12345


class TestEventMetadata:
    def test_all_attributes_set(self, chime, chime_exporter):
        token = chime.track("clustering", id="frb-event-1")
        chime.event_metadata(token, ra=12.4, dec=33.2, classification="FRB")
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_RA] == 12.4
        assert attrs[ATTR_DEC] == 33.2
        assert attrs[ATTR_CLASSIFICATION] == "FRB"

    @pytest.mark.parametrize("classification", ["FRB", "PULSAR", "RFI", "UNKNOWN"])
    def test_classification_values(self, chime, chime_exporter, classification):
        token = chime.track("clustering", id=f"event-{classification}")
        chime.event_metadata(token, ra=0.0, dec=0.0, classification=classification)
        chime.complete(token)
        attrs = finished_spans(chime_exporter)[0].attributes
        assert attrs[ATTR_CLASSIFICATION] == classification
        chime_exporter.clear()


class TestCHIMEEndToEnd:
    def test_full_frb_provenance_chain(self, chime, chime_exporter):
        """data_block → 3 candidates → 1 event with N-to-1 provenance links."""
        block = chime.track("x-engine", id="block-400mhz-001")
        chime.data_block_metadata(block, fpga_rack="rack-3")
        chime.complete(block)

        cand_ids = []
        for beam in [41, 42, 43]:
            cand_id = f"cand-beam{beam}-001"
            t = chime.track("frb-classifier", id=cand_id, parents=["block-400mhz-001"])
            chime.candidate_metadata(t, beam_id=beam, dm=341.2, snr=15.0 + beam * 0.1)
            chime.complete(t)
            cand_ids.append(cand_id)

        event = chime.track("clustering", id="frb-20260415-001", parents=cand_ids)
        chime.event_metadata(event, ra=12.4, dec=33.2, classification="FRB")
        chime.complete(event)

        spans = finished_spans(chime_exporter)
        assert len(spans) == 5  # 1 block + 3 candidates + 1 event

        event_span = next(s for s in spans if s.name == "clustering")
        assert len(event_span.links) == 3
        assert event_span.attributes[ATTR_CLASSIFICATION] == "FRB"
