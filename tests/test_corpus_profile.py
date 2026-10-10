import hashlib
import json
import sqlite3

import pytest

from tools.corpus_profile.profiler import prepare_manifest, final_validation
from tools.corpus_profile.statistics import distribution, summarize


def test_percentiles_and_population_sd():
    result = distribution([1, 2, 3, 4])
    assert result["p50"] == 2.5
    assert result["p10"] == pytest.approx(1.3)
    assert result["standard_deviation_population"] == pytest.approx(1.11803398875)


def test_manifest_portable_identity_and_source_mutation(tmp_path):
    source = tmp_path / "original.pdf"
    source.write_bytes(b"source")
    record = {"path": str(source), "sha256": hashlib.sha256(b"source").hexdigest(),
              "logical_size": 6, "stored_size": 6, "sources": [{"source_id": "one", "detail_url": "https://example.org/one"}]}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"files": [record]}))
    first = prepare_manifest(inventory, tmp_path / "report", 1)
    assert final_validation(first, tmp_path / "report")["source_failures"] == []
    record["path"] = "/another-machine/source.pdf"
    inventory.write_text(json.dumps({"files": [record]}))
    assert prepare_manifest(inventory, tmp_path / "second", 1)["manifest_sha256"] == first["manifest_sha256"]
    source.write_bytes(b"changed")
    assert len(final_validation(first, tmp_path / "report")["source_failures"]) == 1


def test_reject_duplicate_inventory(tmp_path):
    source = tmp_path / "inventory.json"
    row = {"sha256": "same", "logical_size": 1, "sources": []}
    source.write_text(json.dumps({"files": [row, row]}))
    with pytest.raises(ValueError, match="unique SHA"):
        prepare_manifest(source, tmp_path / "report", 2)


def test_all_failed_profiles_remain_visible(tmp_path):
    with sqlite3.connect(tmp_path / "profile.sqlite") as db:
        for name in ["files", "pages", "objects"]:
            db.execute(f"CREATE TABLE {name}(sha TEXT, value TEXT)")
        db.execute("INSERT INTO files VALUES(?,?)", ("bad", json.dumps({"sha256": "bad", "status": "failed", "error": "encrypted", "profile_seconds": .1})))
    result = summarize(tmp_path, {"input_bytes": 100, "manifest_sha256": "manifest"})
    assert result["successful"] == 0
    assert result["failed"][0]["error"] == "encrypted"
    assert result["top_storage_share"]["1"] is None


def test_disk_grouping_separates_intra_and_cross_document_duplicates(tmp_path):
    with sqlite3.connect(tmp_path / "profile.sqlite") as db:
        for name in ["files", "pages", "objects"]:
            db.execute(f"CREATE TABLE {name}(sha TEXT, value TEXT)")
        for sha in ["one", "two"]:
            row = {"sha256": sha, "status": "ok", "source_bytes": 100, "page_count": 1,
                   "sources_json": json.dumps([{"document_type": "PP", "year": 2021}]),
                   "encoded_bytes_by_category": {"image": 20, "font": 0, "content": 0, "other": 0},
                   "residual_file_bytes": 80, "repaired": False, "xref_unresolved": 0,
                   "xref_objects": 2, "profile_seconds": .1, "pdf_version": "1.4", "encrypted": False,
                   "metadata": {}, "warnings": [], "font_program_count": 0,
                   "image_object_count": 2, "content_stream_count": 0}
            db.execute("INSERT INTO files VALUES(?,?)", (sha, json.dumps(row)))
        for sha, xref in [("one", 1), ("one", 2), ("two", 1)]:
            obj = {"xref": xref, "category": "image", "encoded_bytes": 10,
                   "sha256": "same-stream", "file_sha256": sha, "filters": ["FlateDecode"],
                   "dictionary_sha256": "same-dictionary"}
            db.execute("INSERT INTO objects VALUES(?,?)", (sha, json.dumps(obj)))
    summarize(tmp_path, {"input_bytes": 200, "manifest_sha256": "manifest"})
    result = json.loads((tmp_path / "redundancy_summary.json").read_text())
    assert result["intra_duplicate_payload_bytes"] == 10
    assert result["cross_duplicate_payload_bytes"] == 10
    assert result["total_duplicate_payload_bytes_upper_bound"] == 20
    assert result["cross_document_dictionary_fingerprint_groups"] == 1
