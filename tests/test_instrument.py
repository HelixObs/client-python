"""Tests for Instrument — all three integration layers."""

import pytest
from opentelemetry.trace import StatusCode

from tests.conftest import finished_spans


# ── Layer 0: create().start() / complete / error ──────────────────────────────

class TestCreate:
    def test_complete_produces_one_finished_span(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        spans = finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].name == "correlator"

    def test_span_carries_entity_attributes(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        span = finished_spans(exporter)[0]
        assert span.attributes["helix.entity.id"] == "block-1"
        assert span.attributes["helix.instrument.id"] == "TEST"

    def test_complete_sets_ok_status(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.UNSET

    def test_error_adds_helix_error_event(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.error(metadata={"type": "BUFFER_OVERFLOW"})
        span = finished_spans(exporter)[0]
        event_names = [e.name for e in span.events]
        assert "helix.error" in event_names

    def test_error_sets_error_status(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.error()
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.ERROR

    def test_error_metadata_stored_as_event_attributes(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.error(metadata={"type": "TIMEOUT", "rack": "gpu-rack-3"})
        span = finished_spans(exporter)[0]
        helix_event = next(e for e in span.events if e.name == "helix.error")
        assert helix_event.attributes["type"] == "TIMEOUT"
        assert helix_event.attributes["rack"] == "gpu-rack-3"

    def test_complete_is_idempotent(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        token.complete()  # second call is a no-op
        assert len(finished_spans(exporter)) == 1

    def test_error_is_idempotent(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.error()
        token.error()
        assert len(finished_spans(exporter)) == 1

    def test_complete_after_error_is_noop(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.error()
        token.complete()
        spans = finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR

    def test_no_parents_produces_no_links(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        assert finished_spans(exporter)[0].links == ()

    def test_default_parents_is_empty(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.complete()
        span = finished_spans(exporter)[0]
        assert "helix.parent.ids" not in (span.attributes or {})


class TestParentResolution:
    def test_known_parent_becomes_span_link(self, instrument, exporter):
        parent = instrument.create("correlator", id="block-1").start()
        parent.complete()

        child = instrument.create("classifier", id="cand-1", parents=["block-1"]).start()
        child.complete()

        spans = finished_spans(exporter)
        child_span = next(s for s in spans if s.name == "classifier")
        assert len(child_span.links) == 1

    def test_known_parent_link_has_correct_trace_context(self, instrument, exporter):
        parent_token = instrument.create("correlator", id="block-1").start()
        parent_span_ctx = parent_token._span.get_span_context()
        parent_token.complete()

        child = instrument.create("classifier", id="cand-1", parents=["block-1"]).start()
        child.complete()

        child_span = next(s for s in finished_spans(exporter) if s.name == "classifier")
        link_ctx = child_span.links[0].context
        assert link_ctx.trace_id == parent_span_ctx.trace_id
        assert link_ctx.span_id == parent_span_ctx.span_id

    def test_unknown_parent_sets_fallback_attribute(self, instrument, exporter):
        child = instrument.create("classifier", id="cand-1", parents=["block-cross-process"]).start()
        child.complete()
        span = finished_spans(exporter)[0]
        assert span.attributes.get("helix.parent.ids") == "block-cross-process"

    def test_multiple_unknown_parents_joined_with_comma(self, instrument, exporter):
        child = instrument.create("clustering", id="event-1",
                                  parents=["cand-a", "cand-b", "cand-c"]).start()
        child.complete()
        span = finished_spans(exporter)[0]
        assert span.attributes["helix.parent.ids"] == "cand-a,cand-b,cand-c"

    def test_mixed_known_and_unknown_parents(self, instrument, exporter):
        known = instrument.create("correlator", id="block-1").start()
        known.complete()

        child = instrument.create("clustering", id="event-1",
                                  parents=["block-1", "cand-cross"]).start()
        child.complete()

        child_span = next(s for s in finished_spans(exporter) if s.name == "clustering")
        assert len(child_span.links) == 1
        # All parent entity IDs are in the attribute so the gateway can persist
        # the full provenance DAG — not just the unresolved ones.
        assert child_span.attributes["helix.parent.ids"] == "block-1,cand-cross"

    def test_known_parent_also_in_parent_ids_attribute(self, instrument, exporter):
        parent = instrument.create("correlator", id="block-1").start()
        parent.complete()

        child = instrument.create("classifier", id="cand-1", parents=["block-1"]).start()
        child.complete()

        child_span = next(s for s in finished_spans(exporter) if s.name == "classifier")
        assert len(child_span.links) == 1
        assert child_span.attributes["helix.parent.ids"] == "block-1"

    def test_n_to_1_provenance(self, instrument, exporter):
        """N beam candidates → 1 astrophysical event."""
        for i in range(3):
            t = instrument.create("beam-processor", id=f"cand-beam-{i}").start()
            t.complete()

        event = instrument.create(
            "clustering",
            id="frb-event-1",
            parents=["cand-beam-0", "cand-beam-1", "cand-beam-2"],
        ).start()
        event.complete()

        event_span = next(s for s in finished_spans(exporter) if s.name == "clustering")
        assert len(event_span.links) == 3


# ── Layer 1: create() context manager ────────────────────────────────────────

class TestContextManager:
    def test_normal_exit_calls_complete(self, instrument, exporter):
        with instrument.create("classifier", id="cand-1"):
            pass
        span = finished_spans(exporter)[0]
        assert span.status.status_code != StatusCode.ERROR

    def test_exception_calls_error(self, instrument, exporter):
        with pytest.raises(ValueError):
            with instrument.create("classifier", id="cand-1"):
                raise ValueError("bad candidate")
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.ERROR

    def test_exception_adds_helix_error_event(self, instrument, exporter):
        with pytest.raises(RuntimeError):
            with instrument.create("classifier", id="cand-1"):
                raise RuntimeError("classifier timeout")
        span = finished_spans(exporter)[0]
        assert any(e.name == "helix.error" for e in span.events)

    def test_exception_is_not_suppressed(self, instrument):
        with pytest.raises(KeyError):
            with instrument.create("classifier", id="cand-1"):
                raise KeyError("propagated")

    def test_yields_token_for_add_event(self, instrument, exporter):
        with instrument.create("classifier", id="cand-1") as token:
            token.add_event("helix.event.candidate_promoted", {"snr": 23.4})
        finished = finished_spans(exporter)[0]
        event_names = [e.name for e in finished.events]
        assert "helix.event.candidate_promoted" in event_names

    def test_span_attributes_set_correctly(self, instrument, exporter):
        with instrument.create("correlator", id="block-42", parents=[]):
            pass
        span = finished_spans(exporter)[0]
        assert span.attributes["helix.entity.id"] == "block-42"

    def test_parents_resolved_in_context_manager(self, instrument, exporter):
        with instrument.create("correlator", id="block-1"):
            pass
        with instrument.create("classifier", id="cand-1", parents=["block-1"]):
            pass
        cand_span = next(s for s in finished_spans(exporter) if s.name == "classifier")
        assert len(cand_span.links) == 1


# ── Layer 2: create() decorator ──────────────────────────────────────────────

class TestDecorator:
    def test_decorator_with_string_id(self, instrument, exporter):
        @instrument.create("correlator", id="fixed-block")
        def process():
            return 42

        result = process()
        assert result == 42
        assert len(finished_spans(exporter)) == 1

    def test_decorator_complete_on_success(self, instrument, exporter):
        @instrument.create("correlator", id="block-1")
        def process():
            pass

        process()
        assert finished_spans(exporter)[0].status.status_code != StatusCode.ERROR

    def test_decorator_error_on_exception(self, instrument, exporter):
        @instrument.create("correlator", id="block-1")
        def process():
            raise RuntimeError("overflow")

        with pytest.raises(RuntimeError):
            process()
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.ERROR

    def test_decorator_callable_id(self, instrument, exporter):
        @instrument.create("classifier", id=lambda cand: cand["id"], parents=lambda cand: [])
        def classify(cand):
            return "frb"

        classify({"id": "cand-99"})
        span = finished_spans(exporter)[0]
        assert span.attributes["helix.entity.id"] == "cand-99"

    def test_decorator_callable_parents(self, instrument, exporter):
        parent = instrument.create("correlator", id="block-10").start()
        parent.complete()

        @instrument.create(
            "classifier",
            id=lambda cand: cand["id"],
            parents=lambda cand: [cand["block_id"]],
        )
        def classify(cand):
            pass

        classify({"id": "cand-1", "block_id": "block-10"})
        cand_span = next(s for s in finished_spans(exporter) if s.name == "classifier")
        assert len(cand_span.links) == 1

    def test_decorator_preserves_function_name(self, instrument):
        @instrument.create("correlator", id="block-1")
        def my_pipeline_stage():
            pass

        assert my_pipeline_stage.__name__ == "my_pipeline_stage"

    def test_decorator_exception_is_reraised(self, instrument):
        @instrument.create("correlator", id="block-1")
        def failing():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            failing()


# ── Token.add_event ───────────────────────────────────────────────────────────

class TestAddEvent:
    def test_add_event_on_active_token(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.add_event("helix.event.rfi_flagged", {"fraction": "0.92"})
        token.complete()
        span = finished_spans(exporter)[0]
        assert any(e.name == "helix.event.rfi_flagged" for e in span.events)

    def test_add_event_attributes_preserved(self, instrument, exporter):
        token = instrument.create("correlator", id="block-1").start()
        token.add_event("helix.event.archived", {"checksum": "abc123"})
        token.complete()
        span = finished_spans(exporter)[0]
        evt = next(e for e in span.events if e.name == "helix.event.archived")
        assert evt.attributes["checksum"] == "abc123"


# ── operate() ────────────────────────────────────────────────────────────────

class TestOperate:
    def test_operate_produces_finished_span(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-001"):
            pass
        spans = finished_spans(exporter)
        assert len(spans) == 1
        assert spans[0].name == "hdf5-conversion"

    def test_operate_sets_is_operation_attribute(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-001"):
            pass
        span = finished_spans(exporter)[0]
        assert span.attributes.get("helix.entity.is_operation") == "true"

    def test_operate_sets_entity_id_attribute(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-001"):
            pass
        span = finished_spans(exporter)[0]
        assert span.attributes.get("helix.entity.id") == "frb-001"

    def test_operate_sets_instrument_id_attribute(self, instrument, exporter):
        with instrument.operate("registration", entity_id="frb-002"):
            pass
        span = finished_spans(exporter)[0]
        assert span.attributes.get("helix.instrument.id") == "TEST"

    def test_operate_creates_new_root_trace(self, instrument, exporter):
        token = instrument.create("clustering", id="frb-003").start()
        token.complete()
        entity_span = finished_spans(exporter)[0]

        with instrument.operate("hdf5-conversion", entity_id="frb-003"):
            pass
        op_span = finished_spans(exporter)[1]

        assert op_span.context.trace_id != entity_span.context.trace_id

    def test_operate_does_not_overwrite_store(self, instrument, exporter):
        """Entity created before an operation must still be resolvable as parent."""
        token = instrument.create("clustering", id="frb-004").start()
        token.complete()
        entity_ctx = token._span.get_span_context()

        with instrument.operate("hdf5-conversion", entity_id="frb-004"):
            pass

        child = instrument.create("archiving", id="archive-1", parents=["frb-004"]).start()
        child.complete()

        child_span = next(s for s in finished_spans(exporter) if s.name == "archiving")
        assert len(child_span.links) == 1
        assert child_span.links[0].context.trace_id == entity_ctx.trace_id
        assert child_span.links[0].context.span_id == entity_ctx.span_id

    def test_operate_handle_set_attribute(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-005") as op:
            op.set_attribute("helix.chime.hdf5_size_mb", "241.3")
        span = finished_spans(exporter)[0]
        assert span.attributes.get("helix.chime.hdf5_size_mb") == "241.3"

    def test_operate_handle_add_event(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-006") as op:
            op.add_event("helix.event.checkpoint", {"step": "written"})
        span = finished_spans(exporter)[0]
        assert any(e.name == "helix.event.checkpoint" for e in span.events)

    def test_operate_handle_error_adds_helix_error_event(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-007") as op:
            op.error({"message": "disk_full"})
        span = finished_spans(exporter)[0]
        assert any(e.name == "helix.error" for e in span.events)

    def test_operate_handle_error_sets_error_message(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-008") as op:
            op.error({"message": "disk_full"})
        span = finished_spans(exporter)[0]
        err_event = next(e for e in span.events if e.name == "helix.error")
        assert err_event.attributes.get("message") == "disk_full"

    def test_operate_handle_error_sets_error_status(self, instrument, exporter):
        with instrument.operate("hdf5-conversion", entity_id="frb-009") as op:
            op.error({"message": "disk_full"})
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.ERROR

    def test_operate_unhandled_exception_marks_error(self, instrument, exporter):
        with pytest.raises(RuntimeError):
            with instrument.operate("hdf5-conversion", entity_id="frb-010"):
                raise RuntimeError("write_timeout")
        span = finished_spans(exporter)[0]
        assert span.status.status_code == StatusCode.ERROR

    def test_operate_exception_is_not_suppressed(self, instrument):
        with pytest.raises(ValueError):
            with instrument.operate("registration", entity_id="frb-011"):
                raise ValueError("voevent_parse_error")
