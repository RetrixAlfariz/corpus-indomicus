from corpus_indomicus.models import LegalInstrument, SourceObservation
from corpus_indomicus.registry import Registry


def test_registry_tracks_instruments_and_sources(tmp_path):
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

    assert registry.source_seen("test", "https://example.test/27")
    assert registry.hash_seen("abc")
    assert registry.stats() == {
        "instruments": 1,
        "source_observations": 1,
        "unique_content_hashes": 1,
    }
