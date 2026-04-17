# helixobs.chime

CHIME-specific `Instrument` subclass and semantic convention constants.

## What's here

- `CHIMEInstrument` — subclasses `Instrument` with `instrument_id="CHIME"` pre-configured.
- Attribute key constants (e.g. `ATTR_DM`, `ATTR_SNR`, `ATTR_BEAM_ID`) matching the CHIME semantic conventions in the HelixObs design document.
- Metadata helper methods: `data_block_metadata()`, `candidate_metadata()`, `event_metadata()`.

## Semantic convention source

The attribute names here (`helix.chime.*`) are defined in the design document section 4.1.2. Any changes must stay in sync with the gateway's attribute validation and the Grafana dashboard queries.

## Adding new CHIME attributes

1. Add a constant `ATTR_<NAME> = "helix.chime.<name>"`.
2. Add it to the appropriate metadata helper method.
3. Add a test in `tests/test_chime.py`.
