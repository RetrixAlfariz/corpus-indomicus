from __future__ import annotations

import collections
import csv
import json
import math
from pathlib import Path

from .profiler import write_json
from .statistics import distribution, read_rows


def research_answers(summary, redundancy, benchmarks, output):
    total = summary["all"]["bytes"]
    composition = summary["encoded_stream_bytes"]
    dominant = max(composition, key=composition.get)
    rows = list(csv.DictReader((Path(output) / "outlier_analysis.csv").open(encoding="utf-8")))
    full = [r for r in benchmarks if r["input_bytes"] == total and r["successful_units"] == r["units"]]
    independent = [r for r in full if r["mode"] == "perdoc"]
    sharded = [r for r in full if r["mode"] == "shard"]
    lines = [f"**Q1. Dominant storage:** {dominant} encoded payloads occupy {composition[dominant]:,} bytes ({composition[dominant]/total:.2%} of source bytes). Font-program payloads occupy {composition.get('font',0):,} bytes; residual is {summary['residual_file_bytes']:,} bytes."]
    classstats = summary["by_dominant_class"]
    comparisons = []
    for key in ["born", "raster", "raster_text", "mixed", "unknown"]:
        if key in classstats:
            value=classstats[key]
            comparisons.append(f"{key}: {value['files']} PDFs, median {value['bytes_per_page']['median']:,.0f} bytes/page")
    lines.append("**Q2. Raster versus born-digital:** "+"; ".join(comparisons)+". These compare files by majority page class; mixed documents and classifier thresholds limit causal interpretation.")
    image_outliers=sum(int(r["image_stream_bytes"])/int(r["bytes"])>=.70 for r in rows)
    long_outliers=sum(int(r["pages"])>=summary["all"]["page_count"]["p90"] for r in rows)
    lines.append(f"**Q3. Large PDFs:** {image_outliers}/{len(rows)} top outliers are >=70% encoded image bytes; {long_outliers}/{len(rows)} have page counts >= corpus P90. Largest PDF: {summary['all']['file_size']['maximum']:,} bytes. Other outliers contain substantial font/other streams or residual bytes. Incremental indicators and repeated stream payloads are measured separately; titles do not establish table or codec causes.")
    if independent:
        best=min(independent,key=lambda r:r["ratio"])
        lines.append(f"**Q4. Practical byte-exact baseline:** best complete per-document method was {best['method']}, saving {best['saving_pct']:.2f}% ({best['output_bytes']:,} stored bytes). This is a measured standard-codec result on this manifest, not a theoretical compression limit.")
    else:
        lines.append("**Q4. Practical baseline:** no method yet covers all source bytes successfully; partial results cannot establish a full-corpus saving.")
    gains=[]
    for pd in independent:
        for sh in sharded:
            if pd["method"]==sh["method"]:
                gains.append((sh["saving_pct"]-pd["saving_pct"],sh["method"]))
    if gains:
        gain,method=max(gains)
        lines.append(f"**Q5. Cross-document context:** largest same-codec shard improvement is {gain:.2f} percentage points of original bytes for {method}, including index overhead. Cross-document duplicate payload gross bound: {redundancy['cross_duplicate_payload_bytes']:,} bytes. Font and other payloads dominate duplicate-byte potential; visual/semantic similarity was not measured. Shard results justify further context/access experiments, not an unbenchmarked shared-dictionary claim.")
    else:
        lines.append("**Q5. Cross-document context:** matched full-corpus comparisons are pending or unavailable; duplicate payload bounds alone do not establish practical savings.")
    if independent:
        fast=max(independent,key=lambda r:r["encoding_mib_s"])
        slow=min(independent,key=lambda r:r["encoding_mib_s"])
        lines.append(f"**Q6. Cost and access:** per-document encode throughput ranges from {slow['encoding_mib_s']:.2f} MiB/s ({slow['method']}) to {fast['encoding_mib_s']:.2f} MiB/s ({fast['method']}) in this run. The table provides decoding and RSS measurements. Per-document frames preserve independent file access; 128 MiB target shards need prefix decoding and are appropriate only where that access cost is acceptable.")
    lines.append("**Q7. Main bottleneck:** encoded images dominate, with already compressed JPEG/JBIG2/CCITT-family payloads. Image duplicate-byte potential is small compared with fonts/other resources. Therefore neither template similarity nor PDF syntax alone explains the corpus. Measured generic-codec gains include resource repetitions and residual bytes; they do not prove image re-encoding would help or preserve original bytes.")
    lines.append("**Q8. Next phase:** retain the SHA-addressed originals and use the measured per-document zstd baselines as the practical reference. Evaluate cold-storage sharding against retrieval latency before selecting it. For representation research, prioritize exact reconstruction of repeated font/resource payloads and incremental-history bytes, with explicit dictionaries/index costs. Investigate image-stream optimization only with codec-specific evidence and preservation of original encoded payloads. Do not replace originals with rewritten/rendered PDFs.")
    return "\n\n".join(lines)


def aggregate_benchmarks(output):
    output = Path(output)
    rows = []
    for path in sorted((output / "benchmarks").glob("*/benchmark.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(dict(json.loads(line), experiment=path.parent.name))
    csv_rows = list(rows)
    manifest_path = output / "dataset_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        csv_rows.insert(0, {"unit_id": "manifest:" + manifest["manifest_sha256"], "mode": "baseline",
                            "method": "original", "experiment": "original", "status": "baseline",
                            "input_bytes": manifest["input_bytes"], "output_bytes": manifest["input_bytes"],
                            "ratio": 1.0, "saving": 0.0, "manifest_bytes": 0,
                            "error": "no compression/decompression operation; original hashes checked in validation_report.json"})
    if csv_rows:
        keys = sorted({k for row in csv_rows for k in row})
        with (output / "compression_benchmark.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in csv_rows)
    groups = collections.defaultdict(list)
    for row in rows:
        groups[(row["experiment"], row["mode"], row["method"])].append(row)
    results = []
    method_order = {m: i for i, m in enumerate(["gzip9", "zstd3", "zstd9", "zstd19", "lzma2"])}
    for (experiment, mode, method), values in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1], method_order.get(item[0][2], 99))):
        good = [r for r in values if r["status"] == "ok" and r.get("exact")]
        source = sum(r["input_bytes"] for r in good)
        stored = sum(r["output_bytes"] for r in good)
        encode = sum(r["compress_seconds"] for r in good)
        decode = sum(r["decompress_seconds"] for r in good)
        results.append({"experiment": experiment, "mode": mode, "method": method, "units": len(values),
                        "successful_units": len(good), "statuses": dict(collections.Counter(r["status"] for r in values)),
                        "input_bytes": source, "output_bytes": stored,
                        "manifest_bytes": sum(r["manifest_bytes"] for r in good),
                        "ratio": stored/source if source else None, "saving_pct": 100*(1-stored/source) if source else None,
                        "encoding_seconds": encode, "decoding_seconds": decode,
                        "encoding_mib_s": source/1024**2/encode if encode else None,
                        "decoding_mib_s": source/1024**2/decode if decode else None,
                        "peak_rss_bytes": max((r.get("peak_rss_bytes") or 0 for r in good), default=0) or None,
                        "per_unit_saving_distribution": distribution([100*r["saving"] for r in good]),
                        "errors": [{"unit_id": r["unit_id"], "status": r["status"], "error": r.get("error")} for r in values if r not in good]})
    write_json(output / "benchmark_summary.json", results)
    return results


def bars(path, title, labels, values, unit):
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["svg.hashsalt"] = "corpus-profile-2.0.1"
    import matplotlib.pyplot as plt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    axis.bar(range(len(values)), values, color="#327bb3")
    axis.set_xticks(range(len(labels)), labels, rotation=35, ha="right")
    axis.set_title(title, loc="left", pad=15)
    axis.set_xlabel(unit, fontsize=9, labelpad=12)
    axis.grid(axis="y", alpha=.2)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    fig.savefig(path, metadata={"Date": None})
    fig.savefig(path.with_suffix(".png"), dpi=140)
    plt.close(fig)


def generate_figures(output, summary, benchmarks):
    output=Path(output); files=[f for f in read_rows(output,"files") if f["status"]=="ok"]
    edges=[0, .1,.25,.5,1,2,5,10,20,50,100,200,500,1000]
    values=[sum(lo<=f["source_bytes"]/1024**2<hi for f in files) for lo,hi in zip(edges,edges[1:])]
    bars(output/"figures/file_size.svg",f"PDF file size distribution ({len(files)} source PDFs)",[f"{lo:g}-{hi:g}" for lo,hi in zip(edges,edges[1:])],values,"x: MiB bins; y: file count; source bytes")
    pageedges=[0,5,10,20,50,100,200,500,1000,2000,5000]
    values=[sum(lo<=f["page_count"]<hi for f in files) for lo,hi in zip(pageedges,pageedges[1:])]
    bars(output/"figures/page_count.svg","PDF page count distribution",[f"{lo}-{hi}" for lo,hi in zip(pageedges,pageedges[1:])],values,"x: page count bins (lower inclusive); y: file count")
    comp=summary["encoded_stream_bytes"]|{"residual":summary["residual_file_bytes"]}
    bars(output/"figures/storage_composition.svg","Encoded stream storage and residual",list(comp),[v/1024**3 for v in comp.values()],"GiB; residual includes PDF syntax, obsolete revisions, xref, and unaccounted data")
    vals=[r for r in benchmarks if r["saving_pct"] is not None]
    if vals:
        bars(output/"figures/compression_ratio.svg","Standard lossless compression ratio",[r["method"]+"/"+r["mode"] for r in vals],[r["ratio"] for r in vals],"output / source bytes; manifest overhead included for shards; lower is smaller")


def write_report(output):
    output=Path(output)
    summary=json.loads((output/"summary.json").read_text(encoding="utf-8"))
    if not summary["successful"]:
        (output / "RESEARCH_REPORT.md").write_text("# Corpus characterization failed\n\nNo PDF successfully profiled. See summary.json and validation_report.json for explicit failures. No storage conclusions or benchmark claims are supported.\n", encoding="utf-8")
        return []
    redundancy=json.loads((output/"redundancy_summary.json").read_text(encoding="utf-8"))
    validation=json.loads((output/"validation_report.json").read_text(encoding="utf-8"))
    audit=json.loads((output/"visual_audit.json").read_text(encoding="utf-8")) if (output/"visual_audit.json").exists() else {}
    environment=json.loads((output/"performance_environment.json").read_text(encoding="utf-8")) if (output/"performance_environment.json").exists() else {}
    benchmarks=aggregate_benchmarks(output)
    generate_figures(output,summary,benchmarks)
    total=summary["all"]["bytes"]
    lines=["# Corpus Indomicus Research Phase 2", "", "## Dataset and validation", "",
           f"Manifest identity: `{summary['manifest_sha256']}`. Profiler version: `{validation['profiler_version']}`.",
           f"Profiled {summary['profiled']} PDFs; {summary['successful']} successful, {len(summary['failed'])} failed. Original bytes: {total:,}; pages: {summary['all']['pages']:,}.",
           f"Post-run source checksum/size failures: {len(validation['source_failures'])}. Sources were opened read-only. ZIP/7z attachments are outside this PDF manifest.",
           "This is a bounded PP/PMK acquisition sample, not a representative sample of all Indonesian legislation. File counts count attachments, not distinct regulations.", "",
           "## Distribution", "", "| Metric | Mean | Median | P90 | P95 | P99 | Min | Max | Population SD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key in ["file_size","page_count","bytes_per_page"]:
        d=summary["all"][key]
        lines.append("| "+key+" | "+" | ".join(f"{d[k]:,.2f}" for k in ["mean","median","p90","p95","p99","minimum","maximum","standard_deviation_population"])+" |")
    for title, key in [("Document types", "by_type"), ("Regulation years", "by_year")]:
        lines += ["", f"### {title}", "", "| Group | PDFs | Source GB | Pages | Median bytes/PDF | Median pages/PDF | Median bytes/page |", "|---|---:|---:|---:|---:|---:|---:|"]
        for group, metrics in sorted(summary[key].items()):
            lines.append(f"| {group} | {metrics['files']} | {metrics['bytes']/1e9:.3f} | {metrics['pages']:,} | {metrics['file_size']['median']:,.0f} | {metrics['page_count']['median']:.1f} | {metrics['bytes_per_page']['median']:,.0f} |")
    lines += ["", "P10/P25/P50/P75 and all type/year/class breakdowns are in `summary.json`. Sizes are bytes unless stated.",
              "Largest-file storage shares: "+", ".join(f"top {k}% = {summary['top_storage_share'][k]:.2%}" for k in sorted(summary["top_storage_share"], key=int))+".",
              "", "## PDF structure and storage composition", "", "| Category | Encoded payload bytes | Share of original |", "|---|---:|---:|"]
    for category,value in summary["encoded_stream_bytes"].items():
        lines.append(f"| {category} | {value:,} | {value/total:.2%} |")
    lines += [f"| residual | {summary['residual_file_bytes']:,} | {summary['residual_file_bytes']/total:.2%} |", "",
              "Raw encoded stream payloads are counted once per xref. They do not account for all physical PDF bytes. Residual covers syntax, dictionaries, xref tables, obsolete revisions and other unaccounted bytes; it is not a proven removable overhead.",
              f"Repaired PDFs: {summary['repaired_files']}; unresolved xref slots: {summary['unresolved_xref_slots']}. Free/deleted slots and parser failures are not interchangeable. `/Prev` presence is only an incremental-revision indicator, not an exhaustive revision count.",
              f"Encrypted successfully parsed PDFs: {summary.get('encrypted_files', 'unavailable')}; PDFs with meaningful metadata values: {summary.get('files_with_metadata_values', 'unavailable')}; PDFs with parser warnings: {summary.get('files_with_parser_warnings', 'unavailable')}. Header versions, font/resource/content counts and image-dimension distributions are recorded in summary.json and per-file tables.",
              "", "Encoded filter bytes: `"+json.dumps(summary["encoded_filter_bytes"],sort_keys=True)+"`.", "",
              "Image resource colorspaces: `"+json.dumps(summary.get("image_resource_colorspace_occurrences",{}),sort_keys=True)+"`; bit depths: `"+json.dumps(summary.get("image_resource_bit_depth_occurrences",{}),sort_keys=True)+"`. These are page-resource occurrences, not unique images or decoded-pixel identities. Blank/unknown colorspace labels remain explicit.", "",
              "## Page classification", "", "Page counts: `"+json.dumps(summary["page_classes"],sort_keys=True)+"`.",
              "Born: >=20 nonwhitespace text characters and <5% image coverage. Raster: >=70% raster coverage and <20 characters. Raster with selectable text: >=70% coverage and >=20 characters. Mixed: text with 5%-70% image coverage. Remaining pages are unknown. Coverage uses clipped, axis-aligned image placement union; rotation, masks, inline images, and transparency limit precision. Selectable text over raster may be OCR; it does not prove OCR provenance. No OCR was run.",
              f"Visual audit: {len(audit.get('pages', []))} pages; details and disagreements in `visual_audit.json`. Audit is a small deliberate sample, not a corpus-wide ground truth.", "",
              "## Redundancy", "",
              f"Duplicate encoded payload upper bound: {redundancy['total_duplicate_payload_bytes_upper_bound']:,} bytes ({redundancy['fraction_original_bytes']:.3%} of originals). Intra-document: {redundancy['intra_duplicate_payload_bytes']:,}; cross-document: {redundancy['cross_duplicate_payload_bytes']:,}.",
              "Identical encoded payload hashes do not establish identical rendering: object dictionaries and decode parameters can differ. This is a gross upper bound before dictionary/index/container costs. Text-pattern fingerprints normalize whitespace and digits and are neither full-text identity nor semantic deduplication. No visual-image hashing was performed.", "",
              "## Standard byte-exact compression", "",
              "Original baseline: ratio 1.0000, saving 0%; no encode/decode operation. Shards concatenate original PDF bytes with a portable sidecar offset/hash manifest. All successful compressed units verified each reconstructed SHA-256. Decode timing includes checksum computation. Warm local filesystem caches, single trials, and codec-specific startup effects limit performance comparisons. Spawn startup is outside encoding/decoding timing. RSS is a 20ms child-process sample, not a guaranteed exact allocator peak.",
              "", "| Experiment | Mode | Method | Verified units | Input GB | Output GB | Ratio | Saving % | Encode s | Decode s | Enc MiB/s | Dec MiB/s | Peak MiB |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    lines.append(f"| original | independent PDFs | original | {validation['count']-len(validation['source_failures'])}/{validation['count']} sources | {total/1e9:.3f} | {total/1e9:.3f} | 1.0000 | 0.00 | n/a | n/a | n/a | n/a | n/a |")
    for r in benchmarks:
        if r["ratio"] is None:
            lines.append(f"| {r['experiment']} | {r['mode']} | {r['method']} | 0/{r['units']} | unavailable | - | - | - | - | - | - | - | - |")
        else:
            lines.append(f"| {r['experiment']} | {r['mode']} | {r['method']} | {r['successful_units']}/{r['units']} | {r['input_bytes']/1e9:.3f} | {r['output_bytes']/1e9:.3f} | {r['ratio']:.4f} | {r['saving_pct']:.2f} | {r['encoding_seconds']:.2f} | {r['decoding_seconds']:.2f} | {r['encoding_mib_s']:.2f} | {r['decoding_mib_s']:.2f} | {(r['peak_rss_bytes'] or 0)/1024**2:.1f} |")
    lines += ["", "Failures/resource limits and per-unit quantiles are explicitly preserved in `benchmark_summary.json` and `compression_benchmark.csv`. Gzip uses DEFLATE level 9, zstd uses levels 3/9/19, and LZMA2 uses an XZ container at preset 6. These are standard codecs, not a new compression engine.",
              "Shards reduce independent random access: one must decode preceding shard bytes to reach a PDF. Offset manifests locate decoded bytes; they are not seekable compression indexes. Compare per-document and shard results only on matching source coverage. No dictionaries were trained.", "",
              "## Outliers and sampling", "",
              "The 20 largest PDFs have measured page counts, bytes/page, encoded image bytes and full-page raster fractions in `outlier_analysis.csv`. Explanations are measurement-based; table attachments and image codec inefficiency are not inferred from titles. Manual page evidence is kept in the visual audit. Equal allocation over joint type/year/size/page-count/dominant-class strata uses seed 20261010. The top 20 are a separate outlier group. Stratum equal allocation is not an unbiased corpus estimator.", "",
              f"Representative allocation contains {summary['sampling']['representative_files']} PDFs from {summary['sampling']['strata']} observed joint strata; outlier group contains {summary['sampling']['outlier_files']} PDFs. The visual audit is a smaller deliberate page sample from these groups.", "",
              "## Performance environment", "",
              f"CPU: {environment.get('cpu', 'not recorded')}. Platform: {validation['platform']}; Python {validation['python']}. Library versions: `{json.dumps(validation.get('libraries', {}), sort_keys=True)}`.",
              f"Profiling loop: {environment.get('profile_loop_wall_seconds', 'not recorded')} seconds with {environment.get('profile_workers', 'not recorded')} workers, excluding subsequent export/statistics/validation. Sum of per-file worker elapsed checks/parsing: {summary.get('profile_seconds_sum', 0):.2f} seconds; this is not CPU time. Source-byte cap: {environment.get('profile_max_source_bytes', 'not recorded')}. Benchmark concurrency: {environment.get('benchmark_concurrency', 'not recorded')}.",
              "Benchmark caps: 300 seconds and 1 GiB sampled RSS per unit. Missing/limited methods remain explicit. Filesystem caches were not flushed; codecs were measured once; concurrent runs and cache state constrain timing generalization. See performance_environment.json and benchmark metadata for settings.", "",
              "## Research questions and next engineering decision", "",
              research_answers(summary, redundancy, benchmarks, output), "",
              "## Reproduction and limitations", "",
              "See repository `docs/corpus-research.md` for commands, checkpoint identities, resource bounds and interpretation. `dataset_manifest.json` preserves SHA, source URL/source ID/regulation metadata and local paths. Generated reports and benchmark payloads are ignored by Git. Source hashes are checked before profiling and again after research. Per-file parser failures remain visible; heuristic classification is not ground truth. Empirical conclusions are restricted to this manifest. Library/API references: [PyMuPDF document](https://pymupdf.readthedocs.io/en/latest/document.html), [page API](https://pymupdf.readthedocs.io/en/latest/page.html), [zstd streaming](https://python-zstandard.readthedocs.io/en/latest/compressor.html).", ""]
    (output/"RESEARCH_REPORT.md").write_text("\n".join(lines),encoding="utf-8")
    return benchmarks


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument("--output",required=True)
    write_report(parser.parse_args().output)
