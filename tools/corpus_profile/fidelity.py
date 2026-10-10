"""Read-only, deterministic text/visual audit preparation; no OCR accuracy claim."""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from .profiler import digest_file, write_json
from .statistics import read_rows


def content_tags(text, char_count, image_coverage):
    tags = []
    if image_coverage >= .70 and char_count >= 20:
        tags.append("raster_selectable_text")
    if re.search(r"\bPasal\s+\d+", text, re.I):
        tags.append("article")
    if re.search(r"\(\d+\)", text):
        tags.append("paragraph_number")
    if re.search(r"\d", text):
        tags.append("numbers")
    if re.search(r"\b(bagan|struktur organisasi)\b", text, re.I):
        tags.append("diagram_candidate")
    if re.search(r"\b(tabel|tarif|kolom|lokasi|jumlah|satuan)\b", text, re.I):
        tags.append("table_candidate")
        if len(re.findall(r"\d+", text)) >= 50:
            tags.append("numeric_table_candidate")
    if char_count < 20 and image_coverage > .05:
        tags.append("little_extracted_text")
    return tags


def choose_pages(rows, maximum=16):
    """Cover type/size and content candidates before filling deterministic rows."""
    if maximum < 1:
        raise ValueError("maximum must be positive")
    rows = sorted(rows, key=lambda r: (r["sha256"], r["page_number"]))
    chosen = []
    keys = set()
    targets = [("tag", t) for t in ["diagram_candidate", "little_extracted_text", "numeric_table_candidate", "table_candidate",
               "article", "paragraph_number", "raster_selectable_text", "numbers"]]
    targets += [("stratum", (kind, size)) for kind in ["PMK", "PP"] for size in ["small", "medium", "large"]]
    for category, target in targets:
        matches = [r for r in rows if (target in r["tags"] if category == "tag"
                   else (r["stratum"][0], r["stratum"][2]) == target)]
        available = [r for r in matches if (r["sha256"], r["page_number"]) not in keys]
        # Prefer a different document for each target.
        available.sort(key=lambda r: (r["sha256"] in {x["sha256"] for x in chosen}, r["sha256"], r["page_number"]))
        if available and len(chosen) < maximum:
            row = available[0]
            chosen.append(row); keys.add((row["sha256"], row["page_number"]))
    for row in rows:
        if len(chosen) >= maximum:
            break
        key = (row["sha256"], row["page_number"])
        if key not in keys and row["sha256"] not in {x["sha256"] for x in chosen}:
            chosen.append(row); keys.add(key)
    return chosen


def prepare_fidelity(profile, output, maximum=16):
    import pymupdf
    profile, output = Path(profile), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((profile / "dataset_manifest.json").read_text(encoding="utf-8"))
    sample = json.loads((profile / "sample_manifest.json").read_text(encoding="utf-8"))
    entries = {f["sha256"]: f for f in manifest["files"]}
    sampled = {f["sha256"]: f for f in sample["representative"]}
    # Retain only the three previously inspected page locations per sampled PDF.
    file_rows = {f["sha256"]: f for f in read_rows(profile, "files") if f["sha256"] in sampled}
    page_keys = {(sha, n + 1) for sha, f in file_rows.items()
                 for n in {0, f["page_count"] // 2, f["page_count"] - 1}}
    page_rows = {(p["sha256"], p["page_number"]): p for p in read_rows(profile, "pages")
                 if (p["sha256"], p["page_number"]) in page_keys}
    rows, failures = [], []
    for sha in sorted(sampled):
        entry = entries[sha]
        if Path(entry["path"]).stat().st_size != entry["logical_size"] or digest_file(entry["path"]) != sha:
            raise ValueError("source checksum/size mismatch")
        try:
            with pymupdf.open(entry["path"]) as doc:
                for page_number in sorted(n for h, n in page_keys if h == sha):
                    p = doc[page_number - 1]
                    previous = page_rows[(sha, page_number)]
                    text = p.get_text("text", sort=False)
                    sorted_text = p.get_text("text", sort=True)
                    row = {"sha256": sha, "page_number": page_number, "stratum": sampled[sha]["stratum"],
                           "title": entry["sources"][0]["title"], "source_url": entry["sources"][0]["detail_url"],
                           "class_name": previous["class_name"], "image_coverage": previous["image_coverage"],
                           "text_chars": previous["text_chars"], "text": text, "sorted_text": sorted_text,
                           "sort_changes_sequence": text != sorted_text,
                           "vector_path_count": len(p.get_drawings()), "review_status": "not_visually_reviewed"}
                    row["tags"] = content_tags(text, row["text_chars"], row["image_coverage"])
                    rows.append(row)
        except Exception as exc:
            failures.append({"sha256": sha, "error": f"{type(exc).__name__}: {exc}"})
    with (output / "text_sample.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    previews = output / "fidelity_pages"
    previews.mkdir(exist_ok=True)
    selected = choose_pages(rows, maximum)
    plan = []
    for index, row in enumerate(selected):
        preview = previews / f"{index:02d}-{row['sha256'][:12]}-p{row['page_number']}.png"
        with pymupdf.open(entries[row["sha256"]]["path"]) as doc:
            page = doc[row["page_number"] - 1]
            scale = min(120 / 72, 1600 / max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(str(preview))
        item = {k: v for k, v in row.items() if k not in ["text", "sorted_text"]}
        item.update(index=index, preview=str(preview), extracted_excerpt=row["text"][:2500])
        plan.append(item)
    summary = {"manifest_sha256": manifest["manifest_sha256"], "seed": sample["seed"],
               "sample_pdf_count": len(sampled), "sample_page_count": len(rows), "rendered_page_count": len(plan),
               "tags": dict(collections.Counter(t for r in rows for t in r["tags"])),
               "sort_changes_sequence_pages": sum(r["sort_changes_sequence"] for r in rows),
               "failures": failures, "pages": plan,
               "maximum_dpi": 120, "maximum_dimension_pixels": 1600,
               "accuracy": None, "selection": "first/middle/last page of each of 45 joint strata; deliberate visual subset",
               "limitations": ["content tags are candidates, verified during visual review", "no OCR run",
                               "no ground-truth transcription or population accuracy estimate", "sort changes do not prove reading-order errors"]}
    write_json(output / "fidelity_plan.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True); parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=16)
    args = parser.parse_args()
    result = prepare_fidelity(args.profile, args.output, args.max_pages)
    print(json.dumps({k: result[k] for k in ["sample_pdf_count", "sample_page_count", "rendered_page_count", "failures"]}))
