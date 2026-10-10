"""Render deterministic low-resolution audit pages; record human/model review separately."""
from __future__ import annotations

import json
from pathlib import Path

from .profiler import write_json
from .statistics import read_rows


def render_audit(output, max_pages=24):
    import pymupdf
    output = Path(output)
    manifest = json.loads((output / "dataset_manifest.json").read_text(encoding="utf-8"))
    sample = json.loads((output / "sample_manifest.json").read_text(encoding="utf-8"))
    entries = {f["sha256"]: f for f in manifest["files"]}
    sampled = {f["sha256"] for f in sample["representative"]}
    outliers = set(sample["outlier_group"])
    candidates = {}
    for page in read_rows(output, "pages"):
        sha = page["sha256"]
        if sha in sampled or sha in outliers:
            key = (page["class_name"], "outlier" if sha in outliers else "representative")
            # First page for every class/group and largest/smallest-page-count files later.
            candidates.setdefault(key, []).append(page)
    selected = []
    seen = set()
    # Round-robin classes and groups, selecting distinct files before extra pages.
    groups = sorted(candidates)
    for key in groups:
        values = sorted(candidates[key], key=lambda p: (p["sha256"], p["page_number"]))
        count = 0
        for p in values:
            if p["sha256"] not in seen:
                seen.add(p["sha256"]); selected.append(dict(p, audit_group=key[1])); count += 1
            if count >= max(1, max_pages // max(1, len(groups))):
                break
    selected = selected[:max_pages]
    directory = output / "audit_pages"
    directory.mkdir(exist_ok=True)
    rendered = []
    for index, page in enumerate(selected):
        entry = entries[page["sha256"]]
        with pymupdf.open(entry["path"]) as doc:
            source = doc[page["page_number"] - 1]
            image = directory / f"{index:02d}-{page['sha256'][:12]}-p{page['page_number']}.png"
            scale = min(1.0, 1000 / max(source.rect.width, source.rect.height))
            source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(str(image))
        rendered.append({"index": index, "sha256": page["sha256"], "page_number": page["page_number"],
                         "heuristic_class": page["class_name"], "image_coverage": page["image_coverage"],
                         "text_chars": page["text_chars"], "group": page["audit_group"], "preview": str(image),
                         "review_status": "pending"})
    write_json(output / "visual_audit_plan.json", {"maximum_dpi": 72, "maximum_dimension_pixels": 1000, "sampling": "deterministic deliberate class/outlier coverage; not random accuracy estimator", "pages": rendered})
    for start in range(0, len(rendered), 6):
        with pymupdf.open() as sheet:
            canvas = sheet.new_page(width=900, height=1100)
            for position, item in enumerate(rendered[start:start + 6]):
                col, row = position % 2, position // 2
                left, top = col * 450 + 10, row * 365 + 25
                canvas.insert_text((left, top - 8), f"{item['index']:02d} {item['heuristic_class']} {item['group']} p{item['page_number']}", fontsize=10)
                canvas.insert_image(pymupdf.Rect(left, top, left + 425, top + 330), filename=item["preview"], keep_proportion=True)
            canvas.get_pixmap().save(str(directory / f"contact-{start//6:02d}.png"))
    return rendered


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=24)
    args = parser.parse_args()
    print(json.dumps({"rendered_pages": len(render_audit(args.output, args.max_pages))}))
