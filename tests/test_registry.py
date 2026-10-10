from corpus_indomicus.models import DocumentReference, LegalInstrument, SourceObservation
from corpus_indomicus.registry import Registry
from corpus_indomicus.sources.base import DiscoveredDocument
from corpus_indomicus.storage import RawArchive
import json
import sqlite3


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


def test_initialize_migrates_populated_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE instruments (id TEXT PRIMARY KEY, document_type TEXT NOT NULL, number TEXT NOT NULL,
          year INTEGER NOT NULL, title TEXT NOT NULL, jurisdiction TEXT NOT NULL, issuing_body TEXT,
          status TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX idx_instrument_identity ON instruments(jurisdiction, document_type, year, number);
        CREATE TABLE source_records (provider TEXT NOT NULL, source_id TEXT NOT NULL, detail_url TEXT NOT NULL,
          title_hint TEXT, instrument_id TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          last_checked_at TEXT, latest_detail_sha256 TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(provider, source_id));
        CREATE TABLE acquisition_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, provider TEXT NOT NULL,
          from_year INTEGER NOT NULL, to_year INTEGER NOT NULL, snapshot_cutoff TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'discovering', created_at TEXT NOT NULL, started_at TEXT,
          finished_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE run_segments (run_id INTEGER NOT NULL, year INTEGER NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'pending', reported_total INTEGER, discovered_count INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL, PRIMARY KEY(run_id, year));
        CREATE TABLE manifest_items (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
          provider TEXT NOT NULL, source_id TEXT NOT NULL, detail_url TEXT NOT NULL, title_hint TEXT,
          year INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          instrument_id TEXT, last_error TEXT, updated_at TEXT NOT NULL,
          UNIQUE(run_id, provider, source_id));
        INSERT INTO instruments VALUES ('i1','UU','1',2020,'Old','id',NULL,NULL,'{}','t','t');
        INSERT INTO source_records VALUES ('p','s','https://x/s','S','i1','t','t',NULL,NULL,'{}');
        INSERT INTO acquisition_runs(mode,provider,from_year,to_year,snapshot_cutoff,created_at) VALUES ('m','p',2020,2020,'cut','t');
        INSERT INTO run_segments VALUES (1,2020,2,'pending',4,1,'t');
        INSERT INTO manifest_items(run_id,provider,source_id,detail_url,title_hint,year,updated_at) VALUES (1,'p','s','https://x/s','S',2020,'t');
    """)
    conn.commit(); conn.close()
    Registry(path).initialize()
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM run_segments").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM manifest_items").fetchone()[0] == 1
        assert not check.execute("SELECT 1 FROM sqlite_master WHERE name='idx_instrument_identity'").fetchone()


def test_scoped_run_fingerprint_and_membership_dedup(tmp_path):
    registry = Registry(tmp_path / "scope.db"); registry.initialize()
    scope = {"groups": ["pusat"], "types": [{"group": "pusat", "type_id": "6", "name": "A", "reported_total": 1}], "all_years": True, "from_year": None, "to_year": None, "query": None}
    run_id, resumed = registry.create_scoped_run(mode="discover", provider="p", scope=scope, snapshot_cutoff="cut")
    assert not resumed
    scope["types"][0]["reported_total"] = 9
    assert registry.create_scoped_run(mode="discover", provider="p", scope=scope, snapshot_cutoff="cut")[0] == run_id
    scope["query"] = "new"
    assert registry.create_scoped_run(mode="discover", provider="p", scope=scope, snapshot_cutoff="cut")[0] != run_id
    doc = DiscoveredDocument("p", "s", "https://x/s", metadata={"k": "v"})
    assert registry.add_manifest_items(run_id, 0, [doc], group="pusat", type_id="6") == 1
    assert registry.add_manifest_items(run_id, 0, [doc], group="pusat", type_id="6") == 0
    with registry.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_memberships WHERE run_id=?", (run_id,)).fetchone()[0] == 1


def test_freeze_blocks_writes_and_iterator_batches_running_items(tmp_path):
    registry = Registry(tmp_path / "freeze.db"); registry.initialize()
    run_id, _ = registry.create_scoped_run(mode="discover", provider="p", scope={"groups": ["g"], "types": [{"group": "g", "type_id": "1"}], "all_years": True}, snapshot_cutoff="cut")
    docs = [DiscoveredDocument("p", str(i), f"https://x/{i}") for i in range(3)]
    assert registry.add_manifest_items(run_id, 0, docs, group="g", type_id="1") == 3
    registry.update_partition(run_id, "g", "1", 0, status="complete", last_signature="z")
    registry.freeze_manifest(run_id)
    try:
        registry.add_manifest_items(run_id, 0, [docs[0]], group="g", type_id="1")
        assert False, "frozen manifest accepted a write"
    except ValueError:
        pass
    with registry.connect() as conn:
        conn.execute("UPDATE manifest_items SET status='running' WHERE source_id='0'")
    rows = list(registry.iter_manifest_items(run_id, batch_size=1, limit=2))
    assert len(rows) == 2 and rows[0]["source_id"] == "0"


def test_versioned_expected_files_and_coverage(tmp_path):
    registry = Registry(tmp_path / "files.db"); registry.initialize()
    run_id, _ = registry.create_scoped_run(mode="discover", provider="p", scope={"groups": ["g"], "types": [{"group": "g", "type_id": "1"}], "all_years": True}, snapshot_cutoff="cut")
    registry.set_expected_files("p", "s", "d1", ["a.pdf", "b.pdf"])
    registry.record_file("p", "s", "d1", "a.pdf", sha256="ha")
    assert not registry.files_complete("p", "s", "d1")
    registry.record_file("p", "s", "d1", "b.pdf", sha256="hb")
    assert registry.files_complete("p", "s", "d1")
    registry.set_expected_files("p", "s", "d2", ["a.pdf"])
    assert not registry.files_complete("p", "s", "d2")
    registry.update_partition(run_id, "g", "1", 0, status="complete", reported_total=1, discovered_count=1)
    registry.add_manifest_items(run_id, 0, [DiscoveredDocument("p", "s", "https://x/Details/s")], group="g", type_id="1")
    registry.mark_manifest_item(registry.manifest_items(run_id)[0]["id"], "pending", detail_sha256="d2")
    report = registry.coverage(run_id)
    assert report["memberships"] == 1
    assert report["partitions"]["complete"] == 1
    assert report["files"]["expected"] == 1
    assert report["files"].get("downloaded", 0) == 0
    assert registry.coverage()["files"]["downloaded"] == 2
