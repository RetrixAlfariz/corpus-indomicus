import hashlib
import json
import sqlite3

import pymupdf

from tools.corpus_profile.fidelity import choose_pages, content_tags, prepare_fidelity
from tools.corpus_profile.pdf_structure import profile_pdf


def test_candidate_tags_preserve_uncertainty():
    tags = content_tags("Pasal 12 (3) Tarif tabel BAGAN ORGANISASI", 45, .95)
    assert {"article", "paragraph_number", "table_candidate", "diagram_candidate", "raster_selectable_text"} <= set(tags)
    assert content_tags("", 0, .95) == ["little_extracted_text"]


def test_visual_selection_covers_types_sizes_and_is_deterministic():
    rows = [{"sha256": f"{i:02}", "page_number": 1, "stratum": [kind, "2021", size], "tags": []}
            for i, (kind, size) in enumerate((k, s) for k in ["PMK", "PP"] for s in ["small", "medium", "large"])]
    chosen = choose_pages(list(reversed(rows)), 6)
    assert {(r["stratum"][0], r["stratum"][2]) for r in chosen} == {(k, s) for k in ["PMK", "PP"] for s in ["small", "medium", "large"]}
    assert chosen == choose_pages(rows, 6)


def test_missing_text_and_diagram_get_audited_without_duplicate_pages():
    rows = [{"sha256": "one", "page_number": n, "stratum": ["PP", "2020", "medium"], "tags": [tag]}
            for n, tag in [(1, "diagram_candidate"), (2, "little_extracted_text"), (3, "table_candidate")]]
    selected = choose_pages(rows, 3)
    assert len(selected) == 3 and {r["page_number"] for r in selected} == {1, 2, 3}


def test_preparation_retains_source_and_separates_pending_review(tmp_path):
    source = tmp_path / "original.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), "Pasal 12 (3) Tabel 10")
        doc.save(source)
    original = source.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    profile = tmp_path / "profile"
    profile.mkdir()
    entry = {"sha256": sha, "logical_size": len(original), "path": str(source),
             "sources": [{"title": "Fixture", "detail_url": "https://example.org/test"}]}
    (profile / "dataset_manifest.json").write_text(json.dumps({"manifest_sha256": "test", "files": [entry]}))
    (profile / "sample_manifest.json").write_text(json.dumps({"seed": 1, "representative": [{"sha256": sha, "stratum": ["PP", "2021", "small"]}]}))
    data = profile_pdf(source)
    with sqlite3.connect(profile / "profile.sqlite") as db:
        for table in ["files", "pages"]:
            db.execute(f"CREATE TABLE {table}(sha TEXT,value TEXT)")
        db.execute("INSERT INTO files VALUES(?,?)", (sha, json.dumps(dict(data["file"], sha256=sha))))
        for row in data["pages"]:
            db.execute("INSERT INTO pages VALUES(?,?)", (sha, json.dumps(dict(row, sha256=sha))))
    out = tmp_path / "audit"
    summary = prepare_fidelity(profile, out, 2)
    assert source.read_bytes() == original
    assert summary["sample_page_count"] == 1
    assert summary["accuracy"] is None
    assert summary["pages"][0]["review_status"] == "not_visually_reviewed"
    assert (out / "text_sample.jsonl").is_file()
    assert not (out / "text_fidelity_audit.json").exists()
