import hashlib
import json
import sqlite3
import pytest

from tools.corpus_profile.storage_streams import _canonical_name, _dedup_summary, _placement_usage, _font_payload_ref, _load_existing_objects, main


def test_font_name_normalization_preserves_subset_tag():
    assert _canonical_name("/ABCDEF+Arial") == ("Arial", "ABCDEF")
    assert _canonical_name("Arial") == ("Arial", None)


def test_dedup_groups_are_disk_backed_and_split_by_document(tmp_path):
    from tools.corpus_profile.storage_streams import _init_db
    db = _init_db(tmp_path / "x.sqlite")
    payload = hashlib.sha256(b"encoded").hexdigest()
    db.executemany("INSERT INTO objects VALUES(?,?,?,?,?,?)", [
        ("a", 1, "image", 7, payload, "[]"),
        ("a", 2, "image", 7, payload, "[]"),
        ("b", 1, "image", 7, payload, "[]"),
    ])
    summary = _dedup_summary(db)
    assert summary["hash_basis"].startswith("SHA-256 of encoded")
    assert summary["categories"]["image"]["intra_duplicate_payload_bytes"] == 7
    assert summary["categories"]["image"]["cross_duplicate_payload_bytes"] == 7


def test_cli_rejects_missing_manifest(tmp_path):
    with pytest.raises(FileNotFoundError):
        main(["--manifest", str(tmp_path / "missing.json"), "--characterization", str(tmp_path), "--output", str(tmp_path / "out")])


def test_placements_account_for_rotation_multiple_use_and_ambiguity():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page(width=200, height=100)
        image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        image.clear_with(200)
        xref = page.insert_image(page.rect, pixmap=image)
        page.insert_image(pymupdf.Rect(10, 10, 20, 20), xref=xref)
        page.set_rotation(90)
        resources = page.get_images(full=True)
        placed, full, ambiguous = _placement_usage(page, resources)
        assert placed[xref] == 2 and xref in full and not ambiguous
        # Same dimensions/BPC with an alternate resource cannot be uniquely resolved.
        alternate = list(resources[0]); alternate[0] = xref + 100
        placed, full, ambiguous = _placement_usage(page, [resources[0], tuple(alternate)])
        assert not placed and not full and ambiguous == {xref, xref + 100}


def test_font_descriptor_resolution_finds_embedded_payload():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        font = page.insert_font(fontname="Embedded", fontbuffer=pymupdf.Font("helv").buffer)
        payload, kind = _font_payload_ref(doc, font)
        assert payload and kind == "CIDFontType0C"
        assert len(doc.xref_stream_raw(payload)) > 0


def test_font_aliases_do_not_inflate_payload_savings_and_reference_cost_is_measured(tmp_path):
    from tools.corpus_profile.storage_streams import _init_db
    with _init_db(tmp_path / "data.sqlite") as db:
        digest = hashlib.sha256(b"font" * 250).hexdigest()
        db.executemany("INSERT INTO objects VALUES(?,?,?,?,?,?)", [("a", 3, "font", 1000, digest, "[]"), ("b", 3, "font", 1000, digest, "[]")])
        db.executemany("INSERT INTO fonts VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(sha, xref, "TrueType", "Arial", "Arial", None, 3, 1000, digest, "[]", 1) for sha in ["a", "b"] for xref in [1, 2]])
        result = _dedup_summary(db, tmp_path)["categories"]["font"]
    assert result["gross_repeated_payload_bytes"] == 1000
    assert result["serialized_reference_bytes"] == (tmp_path / "duplicate_stream_references.jsonl").stat().st_size
    assert result["capacity_less_reference_bytes"] == 1000 - result["serialized_reference_bytes"]


def test_existing_parquet_filters_are_serialized_for_sqlite(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from tools.corpus_profile.storage_streams import _init_db
    path = tmp_path / "objects.parquet"
    pq.write_table(pa.Table.from_pylist([dict(file_sha256="a", xref=1, category="image", encoded_bytes=1, sha256="encoded", filters=["FlateDecode"])]), path)
    with _init_db(tmp_path / "data.sqlite") as db:
        assert _load_existing_objects(path, db) == 1
        assert json.loads(db.execute("SELECT filters FROM known_objects").fetchone()[0]) == ["FlateDecode"]


def test_font_comparison_excludes_unembedded_resources_with_same_name(tmp_path, monkeypatch):
    import pymupdf
    from tools.corpus_profile.storage_streams import _init_db, _bounded_trials
    payloads = {3: b"first font", 4: b"second font"}

    class Document:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def xref_stream_raw(self, xref): return payloads[xref]

    monkeypatch.setattr(pymupdf, "open", lambda path: Document())
    with _init_db(tmp_path / "data.sqlite") as db:
        rows = [("a", xref, "TrueType", "Arial", "Arial", None, xref, len(raw), hashlib.sha256(raw).hexdigest(), "[]", 1) for xref, raw in payloads.items()]
        rows.append(("a", 5, "Type1", "Arial", "Arial", None, None, 0, None, "[]", 1))
        db.executemany("INSERT INTO fonts VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        trials, comparisons = _bounded_trials(db, [{"sha256": "a", "path": "fixture.pdf"}])
    assert trials == []
    assert len(comparisons) == 1
    assert {member["xref"] for member in comparisons[0]["members"]} == {3, 4}
