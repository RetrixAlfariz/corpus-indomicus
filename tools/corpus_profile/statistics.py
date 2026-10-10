from __future__ import annotations

import collections
import csv
import json
import math
import random
import sqlite3
import statistics
from pathlib import Path

from .profiler import write_json


def distribution(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return {"count": 0}
    def percentile(p):
        pos = (len(values) - 1) * p / 100
        lo = int(pos)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (pos - lo)
    return {"count": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "standard_deviation_population": statistics.pstdev(values), "minimum": values[0], "maximum": values[-1],
            **{f"p{p}": percentile(p) for p in [10, 25, 50, 75, 90, 95, 99]}}


def read_rows(output, table):
    with sqlite3.connect(Path(output) / "profile.sqlite") as db:
        for row in db.execute(f"SELECT value FROM {table} ORDER BY sha"):
            yield json.loads(row[0])


def source_group(row):
    sources = json.loads(row["sources_json"])
    types = sorted({str(s.get("document_type")) for s in sources})
    years = sorted({str(s.get("year")) for s in sources})
    return "/".join(types), "/".join(years)


def summarize(output, manifest, *, seed=20261010, sample_per_stratum=1):
    output = Path(output)
    files = list(read_rows(output, "files"))
    ok = [f for f in files if f["status"] == "ok"]
    page_classes = collections.Counter()
    file_classes = collections.defaultdict(collections.Counter)
    file_full = collections.Counter()
    patterns = collections.defaultdict(lambda: {"pages": 0, "files": set()})
    textchars = []
    coverage = []
    codec_images = collections.Counter()
    image_counts = []
    image_widths, image_heights = [], []
    colorspaces, bit_depths = collections.Counter(), collections.Counter()
    for p in read_rows(output, "pages"):
        page_classes[p["class_name"]] += 1
        file_classes[p["sha256"]][p["class_name"]] += 1
        file_full[p["sha256"]] += p["image_coverage"] >= .70
        textchars.append(p["text_chars"])
        coverage.append(p["image_coverage"])
        image_counts.append(p["image_count"])
        if p.get("text_pattern_fingerprint"):
            group = patterns[p["text_pattern_fingerprint"]]
            group["pages"] += 1
            group["files"].add(p["sha256"])
        for im in p.get("images", []):
            codec_images[im.get("filtercodec") or "unfiltered"] += 1
            colorspaces[str(im.get("colorspace"))] += 1
            bit_depths[str(im.get("bpc"))] += 1
            image_widths.append(im.get("width"))
            image_heights.append(im.get("height"))
    for f in ok:
        f["dominant_class"] = file_classes[f["sha256"]].most_common(1)[0][0] if file_classes[f["sha256"]] else "unknown"
        f["full_page_raster_fraction"] = file_full[f["sha256"]] / max(1, f["page_count"])
        f["bytes_per_page"] = f["source_bytes"] / max(1, f["page_count"])
    composition = collections.Counter()
    object_counts = collections.Counter()
    filters = collections.Counter()
    image_filters = collections.Counter()
    for o in read_rows(output, "objects"):
        composition[o["category"]] += o["encoded_bytes"]
        object_counts[o["category"]] += o["encoded_bytes"] > 0
        filters["+".join(o["filters"]) or "unfiltered"] += o["encoded_bytes"]
        if o["category"] == "image":
            image_filters["+".join(o["filters"]) or "unfiltered"] += o["encoded_bytes"]
    redundancy = {"basis": "SHA-256 of encoded stream payload only; dictionaries and decoding parameters may differ", "categories": {}}
    cross, intra, potential = 0, 0, 0
    # Hash grouping is disk-backed; memory does not grow with object hash count.
    with sqlite3.connect(output / "profile.sqlite") as db:
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-32768")
        db.execute("""CREATE TEMP TABLE streams AS
            SELECT sha file_sha, json_extract(value,'$.sha256') digest,
                   json_extract(value,'$.encoded_bytes') bytes,
                   json_extract(value,'$.category') category
            FROM objects WHERE json_extract(value,'$.encoded_bytes') > 0
        """)
        db.execute("CREATE INDEX temp.idx_stream_digest ON streams(digest,bytes,file_sha)")
        for size, refs, nfiles in db.execute("SELECT bytes,COUNT(*),COUNT(DISTINCT file_sha) FROM streams GROUP BY digest,bytes"):
            intra += size * (refs - nfiles)
            cross += size * (nfiles - 1)
            potential += size * (refs - 1)
        category_duplicates = dict(db.execute("""
            SELECT category,SUM(bytes*(n-1)) FROM (
                SELECT category,bytes,COUNT(*) n FROM streams GROUP BY category,digest,bytes
            ) GROUP BY category
        """))
        for category in composition:
            redundancy["categories"][category] = {"encoded_bytes": composition[category], "duplicate_payload_bytes_upper_bound": category_duplicates.get(category, 0)}
        dictionary_counts = db.execute("""
            SELECT SUM(n>1), SUM(files>1) FROM (
                SELECT COUNT(*) n, COUNT(DISTINCT sha) files
                FROM objects
                WHERE json_extract(value,'$.dictionary_sha256') IS NOT NULL
                GROUP BY json_extract(value,'$.dictionary_sha256')
            )
        """).fetchone()
    redundancy.update(intra_duplicate_payload_bytes=intra, cross_duplicate_payload_bytes=cross,
                      total_duplicate_payload_bytes_upper_bound=potential,
                      fraction_original_bytes=potential / manifest["input_bytes"] if manifest["input_bytes"] else None,
                      repeated_text_pattern_groups=sum(v["pages"] > 1 for v in patterns.values()),
                      cross_document_text_pattern_groups=sum(len(v["files"]) > 1 for v in patterns.values()),
                      repeated_dictionary_fingerprint_groups=dictionary_counts[0] or 0,
                      cross_document_dictionary_fingerprint_groups=dictionary_counts[1] or 0,
                      dictionary_basis="MuPDF normalized dictionary/object syntax; excludes stream payload; not exact source serialization, no storage saving assigned",
                      textual_patterns_basis="first 64 nonempty lines, first 80 chars, whitespace normalized, digits replaced; equality is neither full-text nor semantic equivalence")
    write_json(output / "redundancy_summary.json", redundancy)
    def metrics(rows):
        return {"files": len(rows), "bytes": sum(f["source_bytes"] for f in rows),
                "pages": sum(f["page_count"] for f in rows),
                "file_size": distribution([f["source_bytes"] for f in rows]),
                "page_count": distribution([f["page_count"] for f in rows]),
                "bytes_per_page": distribution([f["bytes_per_page"] for f in rows])}
    grouped = collections.defaultdict(list)
    years = collections.defaultdict(list)
    classes = collections.defaultdict(list)
    for f in ok:
        group, year = source_group(f)
        grouped[group].append(f)
        years[year].append(f)
        classes[f["dominant_class"]].append(f)
    ranked = sorted(ok, key=lambda f: (-f["source_bytes"], f["sha256"]))
    outliers = []
    for rank, f in enumerate(ranked[:20], 1):
        comps = f["encoded_bytes_by_category"]
        image_ratio = comps["image"] / f["source_bytes"]
        reasons = []
        if f["page_count"] >= distribution([x["page_count"] for x in ok])["p90"]:
            reasons.append("high page count (>= corpus P90)")
        if f["full_page_raster_fraction"] >= .70:
            reasons.append("most pages have >=70% raster coverage")
        if image_ratio >= .70:
            reasons.append("encoded image streams dominate file bytes")
        if comps["font"] / f["source_bytes"] >= .20:
            reasons.append("large embedded font-program payload fraction (>=20%)")
        if comps["other"] / f["source_bytes"] >= .20:
            reasons.append("other encoded streams >=20%; resource attribution is incomplete")
        if f["residual_file_bytes"] / f["source_bytes"] >= .10:
            reasons.append("physical bytes outside counted live streams >=10%")
        if f.get("increment_count_lower_bound", 0):
            reasons.append("incremental update indicator present; historical bytes may contribute")
        if f["bytes_per_page"] >= distribution([x["bytes_per_page"] for x in ok])["p90"]:
            reasons.append("high bytes/page (>= corpus P90)")
        if not reasons:
            reasons.append("other structure/residual or page count; inspect measured composition")
        sources = json.loads(f["sources_json"])
        outliers.append({"rank": rank, "sha256": f["sha256"], "bytes": f["source_bytes"], "pages": f["page_count"],
                         "bytes_per_page": f["bytes_per_page"], "image_stream_bytes": comps["image"],
                         "font_stream_bytes": comps["font"], "content_stream_bytes": comps["content"],
                         "other_stream_bytes": comps["other"], "residual_bytes": f["residual_file_bytes"],
                         "xref_objects": f["xref_objects"], "increment_indicator": f.get("increment_count_lower_bound", 0),
                         "full_page_raster_fraction": f["full_page_raster_fraction"], "dominant_class": f["dominant_class"],
                         "measurement_explanation": "; ".join(reasons), "title": sources[0].get("title"),
                         "source_url": sources[0].get("detail_url")})
    with (output / "outlier_analysis.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(outliers[0]) if outliers else ["sha256"])
        writer.writeheader(); writer.writerows(outliers)
    strata = collections.defaultdict(list)
    for f in ok:
        group, year = source_group(f)
        size = "small" if f["source_bytes"] < 1024**2 else "medium" if f["source_bytes"] < 10 * 1024**2 else "large"
        pages = "short" if f["page_count"] <= 10 else "medium" if f["page_count"] <= 100 else "long"
        strata[(group, year, size, pages, f["dominant_class"])].append(f["sha256"])
    rng = random.Random(seed)
    sample = []
    for key, hashes in sorted(strata.items()):
        chosen = sorted(rng.sample(sorted(hashes), min(len(hashes), sample_per_stratum)))
        sample.extend({"sha256": sha, "stratum": list(key), "stratum_population": len(hashes)} for sha in chosen)
    sample_manifest = {"seed": seed, "strategy": "equal allocation per observed joint stratum, not population weighted", "representative": sample,
                       "outlier_group": [f["sha256"] for f in ranked[:20]], "strata_count": len(strata)}
    write_json(output / "sample_manifest.json", sample_manifest)
    summary = {"manifest_sha256": manifest["manifest_sha256"], "profiled": len(files), "successful": len(ok),
               "failed": [{"sha256": f["sha256"], "error": f.get("error")} for f in files if f["status"] != "ok"],
               "all": metrics(ok), "by_type": {g: metrics(fs) for g, fs in sorted(grouped.items())},
               "by_year": {g: metrics(fs) for g, fs in sorted(years.items())},
               "by_dominant_class": {g: metrics(fs) for g, fs in sorted(classes.items())},
               "page_classes": dict(page_classes), "text_chars": distribution(textchars), "image_coverage": distribution(coverage),
               "encoded_stream_bytes": dict(composition), "encoded_filter_bytes": dict(filters),
               "encoded_stream_object_counts": dict(object_counts),
               "encoded_image_filter_bytes": dict(image_filters),
               "image_resource_codec_occurrences": dict(codec_images),
               "image_resource_colorspace_occurrences": dict(colorspaces),
               "image_resource_bit_depth_occurrences": dict(bit_depths),
               "image_resources_per_page": distribution(image_counts),
               "image_resource_width_pixels": distribution(image_widths),
               "image_resource_height_pixels": distribution(image_heights),
               "header_pdf_versions": dict(collections.Counter(f["pdf_version"] for f in ok)),
               "encrypted_files": sum(bool(f["encrypted"]) for f in ok),
               "files_with_metadata_values": sum(any(v for k, v in f["metadata"].items() if k not in {"format", "encryption"}) for f in ok),
               "font_program_count": distribution([f["font_program_count"] for f in ok]),
               "image_resource_object_count": distribution([f["image_object_count"] for f in ok]),
               "content_stream_count": distribution([f["content_stream_count"] for f in ok]),
               "files_with_parser_warnings": sum(bool(f["warnings"]) for f in ok),
               "residual_file_bytes": sum(f["residual_file_bytes"] for f in ok),
               "repaired_files": sum(bool(f["repaired"]) for f in ok), "unresolved_xref_slots": sum(f["xref_unresolved"] for f in ok),
               "top_storage_share": {str(p): sum(f["source_bytes"] for f in ranked[:math.ceil(len(ok)*p/100)]) / sum(f["source_bytes"] for f in ok) if ok else None for p in [1, 5, 10]},
               "profile_seconds_sum": sum(f["profile_seconds"] for f in files),
               "profile_seconds_distribution": distribution([f["profile_seconds"] for f in files]),
               "sampling": {"strata": len(strata), "representative_files": len(sample), "outlier_files": min(20, len(ok))}}
    write_json(output / "summary.json", summary)
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild deterministic statistics from profile checkpoints")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    manifest = json.loads((output / "dataset_manifest.json").read_text(encoding="utf-8"))
    result = summarize(output, manifest)
    print(json.dumps({"successful": result["successful"], "failed": len(result["failed"])}))
