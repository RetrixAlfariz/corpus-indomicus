from corpus_indomicus.models import DocumentReference, LegalInstrument, SourceObservation
from corpus_indomicus.registry import Registry
from corpus_indomicus.sources.base import DiscoveredDocument
from corpus_indomicus.storage import RawArchive


def test_registry_tracks_objects_sources_and_references(tmp_path):
    registry = Registry(tmp_path / "corpus.db")
    registry.initialize()
    instrument = LegalInstrument.from_minimal(
        document_type="UU", number="27", year=2022, title="PDP"
    )
    registry.upsert_instrument(instrument)

    obj = RawArchive(tmp_path / "objects").store(
        provider="test", source_url="https://example.test/27",
        data=b"%PDF-1.7\nx", retrieved_at="2026-08-24T00:00:00+00:00",
        content_type="application/pdf",
    )
    registry.record_object(obj)
    registry.record_source(
        SourceObservation(
            provider="test", source_url="https://example.test/27",
            content_sha256=obj.sha256,
        ), instrument_id=instrument.id,
    )
    registry.record_reference(
        DocumentReference(
            provider="test", source_id="27", source_url="https://example.test/27",
            target_source_id="1", target_url="https://example.test/1",
        ), instrument_id=instrument.id,
    )
    assert registry.hash_seen(obj.sha256)
    assert registry.stats()["instruments"] == 1
    assert registry.stats()["document_references"] == 1


def test_finite_manifest_is_deduplicated_and_resumeable(tmp_path):
    registry = Registry(tmp_path / "corpus.db")
    registry.initialize()
    run_id = registry.create_run(
        mode="backfill", provider="jdih_bpk", from_year=2025, to_year=2026,
        snapshot_cutoff="2026-08-24T04:00:00+07:00",
    )
    docs = [
        DiscoveredDocument("jdih_bpk", "1", "https://x/Details/1", "A"),
        DiscoveredDocument("jdih_bpk", "1", "https://x/Details/1", "A"),
    ]
    assert registry.add_manifest_items(run_id, 2025, docs) == 1
    assert registry.manifest_count(run_id) == 1
    row = registry.find_resumable_run(
        mode="backfill", provider="jdih_bpk", from_year=2025, to_year=2026
    )
    assert row is not None
    assert int(row["id"]) == run_id
