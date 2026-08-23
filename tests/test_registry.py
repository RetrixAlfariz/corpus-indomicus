from corpus_indomicus.models import DocumentReference, LegalInstrument, SourceObservation
from corpus_indomicus.registry import Registry


def test_registry_tracks_instruments_sources_and_references(tmp_path):
    registry = Registry(tmp_path / "corpus.db")
    registry.initialize()

    instrument = LegalInstrument.from_minimal(
        document_type="UU",
        number="27",
        year=2022,
        title="Pelindungan Data Pribadi",
    )
    registry.upsert_instrument(instrument)
    registry.record_source(
        SourceObservation(
            provider="test",
            source_url="https://example.test/27",
            content_sha256="abc",
        ),
        instrument_id=instrument.id,
    )

    reference = DocumentReference(
        provider="test",
        source_id="27",
        source_url="https://example.test/27",
        target_source_id="12",
        target_url="https://example.test/12",
        target_label="UU No. 12",
        context_label="Status",
        raw_context="Status UU No. 12",
    )
    registry.record_reference(reference, instrument_id=instrument.id)
    registry.record_reference(reference, instrument_id=instrument.id)

    assert registry.source_seen("test", "https://example.test/27")
    assert registry.hash_seen("abc")
    assert registry.stats() == {
        "instruments": 1,
        "source_observations": 1,
        "document_references": 1,
        "unique_content_hashes": 1,
    }
