"""Combine measured Phase 2.1 evidence without inferring untested savings."""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
from pathlib import Path

from .profiler import write_json
from .storage_streams import VERSION as STREAM_VERSION


def aggregate_benchmarks(rows, experiment):
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[(row["mode"], row["method"])].append(row)
    result = []
    for (mode, method), values in sorted(grouped.items()):
        good = [v for v in values if v.get("status") == "ok" and v.get("exact") is True]
        original = sum(v["input_bytes"] for v in good)
        packed = sum(v["output_bytes"] for v in good)
        encode = sum(v["compress_seconds"] for v in good)
        decode = sum(v["decompress_seconds"] for v in good)
        result.append({"experiment": experiment, "mode": mode, "method": method, "units": len(values),
                       "successful_units": len(good), "input_bytes": original, "output_bytes": packed,
                       "manifest_bytes": sum(v["manifest_bytes"] for v in good),
                       "saving_pct": 100 * (1 - packed / original) if original else None,
                       "encoding_seconds": encode, "decoding_seconds": decode,
                       "encoding_mib_s": original / 1024**2 / encode if encode else None,
                       "decoding_mib_s": original / 1024**2 / decode if decode else None,
                       "peak_rss_bytes": max((v.get("peak_rss_bytes") or 0 for v in good), default=0)})
    return result


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bench_rows(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"] + [
        "| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in row) + " |" for row in rows]


def verify_evidence_identity(manifest, streams, audit):
    if any(x.get("manifest_sha256") != manifest["manifest_sha256"] for x in [streams, audit]):
        raise ValueError("evidence manifest mismatch")
    if streams.get("profiled") != manifest["count"] or streams.get("failed"):
        raise ValueError("stream analysis does not cover the complete manifest")
    if streams.get("profiler_version") != STREAM_VERSION:
        raise ValueError("stream profiler version mismatch")
    if not audit.get("pages") or any(r.get("review_status") != "reviewed" for r in audit["pages"]):
        raise ValueError("visual audit contains pending pages")


def generate(profile, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyarrow.parquet as pq

    profile, output = Path(profile), Path(output)
    manifest = _read(profile / "dataset_manifest.json")
    summary = _read(profile / "summary.json")
    redundancy = _read(profile / "redundancy_summary.json")
    streams = _read(output / "storage-feasibility-summary.json")
    audit = _read(output / "text_fidelity_audit.json")
    baseline = _read(output / "feasibility_summary.json")
    verify_evidence_identity(manifest, streams, audit)
    if not baseline["benchmark_validation"]["full_coverage"] or not baseline["validation"]["full_coverage"]:
        raise ValueError("sample baseline is incomplete or failed")
    if not baseline["full_benchmark_reference"]["reused_without_rerun"]:
        raise ValueError("full baseline compatibility/validation failed")
    validation = _read(output / "validation_report.json")
    if validation["source_failures"] or validation["manifest_sha256"] != manifest["manifest_sha256"]:
        raise ValueError("final source validation failed")
    benchmark = aggregate_benchmarks(_bench_rows(output / "benchmarks/sample/benchmark.jsonl"), "sample45")
    for name in ["full-perdoc", "full-shard"]:
        benchmark += aggregate_benchmarks(_bench_rows(profile / "benchmarks" / name / "benchmark.jsonl"), "full1000")
    with (output / "compression_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(benchmark[0])); writer.writeheader(); writer.writerows(benchmark)
    codec = summary["encoded_image_filter_bytes"]
    total = manifest["input_bytes"]
    with sqlite3.connect(f"file:{(output / 'storage-feasibility.sqlite').resolve().as_posix()}?mode=ro", uri=True) as db:
        font_types = {k or "unknown": v for k, v in db.execute("SELECT font_type,COUNT(*) FROM fonts GROUP BY font_type")}
        font_counts = dict(db.execute("SELECT embedded,COUNT(*) FROM fonts GROUP BY embedded"))
        subsets = db.execute("SELECT COUNT(*) FROM fonts WHERE subset_tag IS NOT NULL").fetchone()[0]
        image_count = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        doc_types = {f['sha256']: '/'.join(sorted({str(s.get('document_type') or 'unknown') for s in f['sources']})) for f in manifest['files']}
        codec_types = collections.Counter()
        for sha, name, size in db.execute("SELECT file_sha,codec,SUM(encoded_bytes) FROM images GROUP BY file_sha,codec"):
            codec_types[(doc_types[sha], name or "unfiltered")] += size
        actual_image_bytes = db.execute("SELECT SUM(encoded_bytes) FROM images").fetchone()[0]
        if actual_image_bytes != sum(codec.values()):
            raise ValueError("image stream inventory does not reconcile with Phase 2 image payload bytes")
        unique_font_hashes = db.execute("SELECT COUNT(DISTINCT encoded_sha256) FROM objects WHERE category='font' AND encoded_bytes>0").fetchone()[0]
        top_fonts = []
        for digest, size, count, docs in db.execute("SELECT encoded_sha256,MAX(encoded_bytes),COUNT(*),COUNT(DISTINCT file_sha) FROM objects WHERE category='font' AND encoded_bytes>0 GROUP BY encoded_sha256 ORDER BY SUM(encoded_bytes) DESC LIMIT 10"):
            names = [n or 'unknown' for n, in db.execute("SELECT DISTINCT normalized_name FROM fonts WHERE payload_sha256=? LIMIT 5", (digest,))]
            top_fonts.append(dict(encoded_sha256=digest, names=names, encoded_size=size, payload_xrefs=count, documents=docs, stored_bytes=size*count))
    figures = output / "figures"
    figures.mkdir(exist_ok=True)
    def save(name):
        plt.tight_layout(); plt.savefig(figures / (name + ".png"), dpi=140)
        plt.savefig(figures / (name + ".svg")); plt.close()
    ordered = sorted(codec.items(), key=lambda x: x[1], reverse=True)
    plt.figure(figsize=(10, 5)); plt.barh([k for k, _ in reversed(ordered)], [v / 10**9 for _, v in reversed(ordered)])
    plt.xlabel("Encoded image payload GB (decimal)"); plt.title("Image storage by filter chain"); save("image_codec_storage")
    plt.figure(figsize=(9, 5)); cats = redundancy["categories"]
    plt.bar(list(cats), [v["duplicate_payload_bytes_upper_bound"] / 10**6 for v in cats.values()])
    plt.ylabel("Repeated encoded payload MB; gross bound"); plt.title("Byte-identical stream redundancy, before index costs"); save("stream_redundancy")
    full = [r for r in benchmark if r["experiment"] == "full1000"]
    plt.figure(figsize=(10, 5)); methods = ["gzip9", "zstd3", "zstd9", "zstd19", "lzma2"]
    for offset, mode in [(-.18, "perdoc"), (.18, "shard")]:
        values = {r["method"]: r["saving_pct"] for r in full if r["mode"] == mode}
        plt.bar([i + offset for i in range(len(methods))], [values[m] for m in methods], width=.36, label=mode)
    plt.xticks(range(len(methods)), methods); plt.ylabel("Saving % of original bytes"); plt.title("Complete 1000-PDF baseline, shard index included")
    plt.legend(); save("full_baseline")
    table = pq.read_table(profile / "file_profile.parquet", columns=["source_bytes", "page_count", "status"]).to_pylist()
    ok = [r for r in table if r["status"] == "ok"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    axes[0].hist([r["source_bytes"] / 10**6 for r in ok], bins=40); axes[0].set_xlabel("PDF size MB")
    axes[1].hist([r["page_count"] for r in ok], bins=40); axes[1].set_xlabel("Pages per PDF")
    for ax in axes: ax.set_ylabel("PDF count")
    fig.suptitle("Frozen corpus distributions"); save("distributions")
    write_json(output / "research_evidence.json", {"manifest_sha256": manifest["manifest_sha256"], "image_objects": image_count,
               "font_resource_rows_by_type": font_types, "font_resource_rows_by_embedded": font_counts,
               "subset_named_font_resource_rows": subsets, "benchmarks": benchmark,
               "source_validation_failures": len(validation["source_failures"]), "fidelity_accuracy": None,
               "top_font_payloads": top_fonts, "unique_font_payload_hashes": unique_font_hashes})
    lines = ["# Storage optimization feasibility: Phase 2.1", "",
             "## Scope and validation", "",
             f"Frozen local manifest: `{manifest['manifest_sha256']}`; Phase 2 profiler {manifest['profiler_version']}. "
             f"{manifest['count']:,} unique PDFs, {total:,} original bytes, {summary['all']['pages']:,} pages. "
             "No source rewrite, OCR, acquisition/backfill, new compression engine, or upload of PDFs was performed.", "",
             "SHA-256 and size pre/post verification passed; parse failures remain explicit in stream summaries. "
             "The new 45-PDF stratified standard-codec run is separately measured. The compatible complete Phase 2 runs are revalidated and reused, not falsely reported as new executions.", "",
             "## Distribution and representation", ""]
    lines += _table(["Metric", "Mean", "Median", "P90", "Maximum"], [[name, f"{summary['all'][key]['mean']:,.2f}", f"{summary['all'][key]['median']:,.2f}", f"{summary['all'][key]['p90']:,.2f}", f"{summary['all'][key]['maximum']:,}"] for name, key in [("PDF bytes", "file_size"), ("Pages", "page_count"), ("Bytes/page", "bytes_per_page")]])
    lines += ["", f"Top 10% PDFs account for {summary['top_storage_share']['10'] * 100:.2f}% of source bytes. "
              "Source-record years are provenance labels; the visual audit found an older regulation inside an attachment attached to a later record. They are not guaranteed document-content years.", "",
              "## Image streams", ""]
    lines += _table(["Filter chain", "Encoded bytes", "% corpus", "% image payload"], [[k, f"{v:,}", f"{100*v/total:.3f}", f"{100*v/sum(codec.values()):.3f}"] for k, v in ordered])
    jpeg_family = sum(v for k, v in codec.items() if 'DCTDecode' in k.split('+'))
    lines += ["", f"The largest single filter-chain category is {ordered[0][0]} ({ordered[0][1]:,} bytes). "
              f"JPEG/DCT across direct and Flate-wrapped chains totals {jpeg_family:,} bytes ({100*jpeg_family/total:.2f}% of originals). "
              "A filter-chain count and a codec-family count answer different questions; wrapped JPEG is included only once."]
    lines += ["", f"Image stream records: {image_count:,}. Image/font/object rows and page usage are stored in storage-feasibility.sqlite. "
              "Hash basis is raw encoded stream bytes, not decoded pixels. Identical bytes can have different dictionaries or decoding parameters; visual equivalence is not asserted.", "",
              "Full filter chains are preserved; JPEG behind Flate is not a separate image codec. Already-compressed formats are candidates for measured wrapping trials, not proof of optimal compression.", "",
              "## Font and object redundancy", ""]
    insert = lines.index("## Font and object redundancy")
    lines[insert:insert] = [f"Placement-accounting diagnostics: `{json.dumps(streams.get('image_usage', {}), sort_keys=True)}`. "
                           "Unresolved placements do not prove that a resource is unused. Same-dimension resource aliases remain ambiguous; individual rotated bbox coverage flags possible scans rather than visual ground truth.", ""]
    insert = lines.index("## Font and object redundancy")
    lines[insert:insert] = ["### Codec distribution by source document type", ""] + _table(
        ["Source type", "Terminal image filter", "Encoded bytes", "% corpus"],
        [[kind, name, f"{size:,}", f"{100*size/total:.3f}"] for (kind, name), size in sorted(codec_types.items())]) + ["", "Source-type labels follow the inventory. This counts all image payload xrefs once; terminal-filter grouping is separate from the complete filter-chain table above.", ""]
    lines += _table(["Category", "Encoded bytes", "Repeated bytes: gross bound", "% corpus"], [[k, f"{v['encoded_bytes']:,}", f"{v['duplicate_payload_bytes_upper_bound']:,}", f"{100*v['duplicate_payload_bytes_upper_bound']/total:.3f}"] for k, v in cats.items()])
    lines += ["", f"Font resource rows by type: `{json.dumps(font_types, sort_keys=True)}`. Embedded flags: `{font_counts}`; subset-named resource rows: {subsets:,}. "
              "These count resource/descriptor entries; byte totals count each payload xref once. Shared names do not prove payload identity.", "",
              f"Cross-document repeated encoded bytes: {redundancy['cross_duplicate_payload_bytes']:,}; intra-document: {redundancy['intra_duplicate_payload_bytes']:,}. "
              "The gross repeated-payload bound is not net container savings. Original byte positions, syntax, dictionaries, revisions and all omitted bytes must remain reconstructible. "
              "Stream equality and reference-cost estimates do not establish a working byte-exact PDF transformation. No transformed PDF/storage engine was implemented.", "",
              "Nonidentical font candidates and their comparison limits are in storage-feasibility-summary.json. Any equality claim is based on payload bytes, never font name alone.", "",
              "## Text-layer fidelity", "",
              f"Starting sample: {audit['sample_pdf_count']} PDFs, {audit['sample_page_count']} first/middle/last pages; seed {audit['seed']}. "
              f"Visual review: {len(audit['pages'])} deliberately selected pages at <=120 DPI / 1600 px, covering PMK/PP and small/medium/large strata, legal numbers, tables and diagrams. "
              "No full ground-truth transcription exists; no corpus OCR accuracy percentage is estimated.", ""]
    insert = lines.index("### Measured reference costs") if "### Measured reference costs" in lines else lines.index("## Text-layer fidelity")
    font_section = ["### Largest repeated embedded font payloads", "", f"Physical font-program stream xrefs: {streams['font_unique_payload_xrefs']:,}; distinct encoded SHA values: {unique_font_hashes:,}. "
                    "Subset-name prefixes are only naming indicators, not a glyph-subset proof.", ""] + _table(
        ["Name labels", "Encoded bytes each", "Payload xrefs", "Documents", "Stored bytes"],
        [[', '.join(r['names']), f"{r['encoded_size']:,}", r['payload_xrefs'], r['documents'], f"{r['stored_bytes']:,}"] for r in top_fonts])
    font_section += ["", f"The top two byte-distinct payloads account for {100*sum(r['stored_bytes'] for r in top_fonts[:2])/cats['font']['encoded_bytes']:.2f}% of encoded font bytes. "
                     "Identity is determined by raw payload SHA, not these name labels. Repeated embedding explains the large font total more directly than template similarity.", ""]
    lines[insert:insert] = font_section
    insert = lines.index("## Text-layer fidelity")
    capacity = streams['categories']
    extra = ["### Measured reference costs", ""] + _table(
        ["Category", "Gross repeated bytes", "Serialized reference bytes", "Conditional capacity after references"],
        [[k, f"{v['gross_repeated_payload_bytes']:,}", f"{v['serialized_reference_bytes']:,}", f"{v['capacity_less_reference_bytes']:,}"] for k, v in capacity.items()])
    extra += ["", "References are actually serialized UTF-8 JSONL. The conditional remainder excludes original-byte offset/serialization/revision mapping costs, full index headers and any compression interplay. It is neither a measured net storage saving nor a proven lower bound.", ""]
    similarity = streams.get('font_similarity', [])
    extra += ["### Nonidentical font payload comparisons", ""] + _table(
        ["Normalized name", "Encoded size ratio", "1 KiB block Jaccard"],
        [[r['normalized_name'], f"{r['size_ratio']:.4f}", f"{r['encoded_1kib_chunk_jaccard']:.4f}"] for r in similarity])
    extra += ["", "At most ten deterministic same-name pairs ranked by resource-reference bytes, encoded payloads <=1 MiB each. This ranking selects audit candidates, not a storage estimate. Shared aligned encoded blocks are binary evidence only; compression can hide similarity and these values do not describe glyph identity. No savings are assigned to nonidentical candidates.", ""]
    lines[insert:insert] = extra
    trials = streams.get('stream_wrapping_trials', [])
    if trials:
        insert = lines.index("## Text-layer fidelity")
        trial_table = _table(["Codec", "Streams", "Input bytes", "Wrapped bytes", "Saving %", "Exact restored"],
                            [[t['codec'], t['streams'], f"{t['input_bytes']:,}", f"{t['output_bytes']:,}",
                              f"{100*(1-t['output_bytes']/t['input_bytes']):.3f}", t['verified_sha256']] for t in trials])
        lines[insert:insert] = ["### Bounded raw-image stream wrapping trials", "", "Standard zstd9 wraps original encoded payloads; no image decode/re-encode is performed. These bounded trials do not estimate all images or establish a new PDF representation.", ""] + trial_table + ["", "Candidate selection/caps and excluded payloads are recorded in storage-feasibility-summary.json. Small or negative wrapping gains support leaving those tested payloads in their original encoding; larger gains justify targeted follow-up only.", ""]
    lines += _table(["PDF SHA prefix / page", "Finding", "Qualitative visual comparison"], [[f"{r['sha256'][:12]} / {r['page_number']}", r['finding'], r['observation']] for r in audit['pages']])
    lines += ["", "Examples and reviewed previews are linked from text_fidelity_audit.json; complete extracted/sorted text remains local in text_sample.jsonl. "
              "Sorting differences are diagnostic only, not automatic proof of an error. A text-only representation loses nontext marks, table/diagram topology and exact original serialization even where words are correct.", "",
              "## Standard compression baseline", "",
              "Original baseline: 4,830,705,402 bytes, ratio 1, savings 0; no encode/decode. Gzip 9, zstd 3/9/19 and LZMA2 in XZ preset 6 were measured in the complete Phase 2 run. "
              "The sample methods actually run are recorded below, including index overhead for shards.", ""]
    lines += _table(["Experiment", "Mode", "Codec", "OK/units", "Input GB", "Output GB", "Save %", "Enc s", "Dec s", "Enc MiB/s", "Dec MiB/s", "RSS MiB"],
                    [[r['experiment'], r['mode'], r['method'], f"{r['successful_units']}/{r['units']}", f"{r['input_bytes']/1e9:.3f}", f"{r['output_bytes']/1e9:.3f}", f"{r['saving_pct']:.2f}", f"{r['encoding_seconds']:.2f}", f"{r['decoding_seconds']:.2f}", f"{r['encoding_mib_s']:.2f}", f"{r['decoding_mib_s']:.2f}", f"{r['peak_rss_bytes']/1024**2:.1f}"] for r in benchmark])
    lines += ["", "All successful units verify reconstructed SHA-256 for each member PDF. Per-document compression preserves independent access; target 128 MiB shards need prefix decoding. "
              "Sidecar offsets refer to decoded bytes and are not a seekable compression index. Runs are single warm-cache local measurements; RSS is sampled, codec startup is outside timed sections, decode includes checksum work, and timing is not a general hardware-independent guarantee.", "",
              "## Ranked next research opportunities", ""]
    insert = lines.index("## Ranked next research opportunities")
    lines[insert:insert] = ["### Execution resources", "", f"Stream settings: `{json.dumps(streams.get('resource_limits', {}), sort_keys=True)}`. "
                           "One source PDF is inspected at a time; SQL group operations use disk. Peak RSS is measured only for the standard whole-PDF/shard benchmark, not claimed for stream trials or parser internals. "
                           f"Stream trial coverage: `{json.dumps(streams.get('trial_coverage', {}), sort_keys=True)}`. "
                           f"Library versions: `{json.dumps(validation.get('libraries', {}), sort_keys=True)}`; Python {validation.get('python')}, {validation.get('platform')}. "
                           "Cache state was not reset; sample performance is not an unbiased corpus estimate.", ""]
    lines += _table(["Rank", "Opportunity", "Evidence / potential benefit", "Information risk", "Implementation cost / next experiment"], [
        [1, "Standard per-PDF zstd storage", "Measured full-corpus zstd9 saves 10.09%; zstd3 9.38% with faster encoding", "Low if source bytes/SHA and error handling preserved", "Low; measure retrieval latency and operational overhead before adoption"],
        [2, "Cold-storage context/shards", "Measured zstd9 shard saves 13.22%; zstd19 15.23%, including index", "Low byte loss risk with receipts; increased retrieval work", "Medium; latency/cache/failure isolation study, not new codec"],
        [3, "Exact repeated font/resource payload representation", "Font repeated payload gross bound 337,404,575 bytes (6.98% corpus); other streams also repeat", "High unless syntax/positions/revisions/dictionaries all reconstruct", "High; first specify a reversible mapping and measure index costs against zstd; benefit cannot be added to generic compression gains"],
        [4, "Codec-specific reversible image wrappers", "Images dominate bytes, but image byte duplication is only 0.243% corpus; small trials remain codec/sample specific", "High if decoding/re-encoding changes original payload; decoded-pixel equality insufficient", "Medium/high; preserve raw encoded bytes, test bounded deterministic candidates with standard codecs"],
        [5, "Text as a derived search representation", "Audit demonstrates missing table text, corrupted numbers and reading/diagram order", "Unacceptable as raster/PDF replacement; useful alongside originals", "Medium; quality flags and human verification, no source reduction claim"]])
    lines += ["", "## Limits and decision gate", "",
              "Results apply to these 716 PMK / 284 PP PDFs, not all Indonesian regulations. Page classification is heuristic. "
              "50,099 unresolved xref slots may include free/deleted/unreadable entries; zero repaired or parser-failed PDFs does not prove every historical object was examined. "
              "Image coverage is an axis-aligned estimate with limits for masks, clipping, inline images and transparency. Decoded image hashes were not computed. "
              "Residual bytes are not proven removable. Deduplication capacity is not additive with standard-codec savings. No new engine is authorized before review of this evidence.", "",
              "See docs/corpus-storage-feasibility.md for repeatable commands, outputs, sample/full-run validation and resource limits.", ""]
    lines += ["## Charts", "", "![Image codec storage](figures/image_codec_storage.png)", "",
              "![Stream redundancy](figures/stream_redundancy.png)", "",
              "![Complete compression baseline](figures/full_baseline.png)", "",
              "![Source size distributions](figures/distributions.png)", ""]
    (output / "RESEARCH_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return benchmark


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--profile", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(); print(json.dumps({"benchmark_rows": len(generate(args.profile, args.output))}))
