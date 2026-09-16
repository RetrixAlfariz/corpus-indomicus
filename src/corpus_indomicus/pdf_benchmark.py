"""Reproducible, non-destructive compression experiments for PDF masters.

The benchmark deliberately treats the input paths as read-only.  Compressed
artifacts and reconstructed files are placed below ``output_dir``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import mean, median
from typing import Any


_METHODS = ("original", "gzip9", "zstd9", "7z_lzma2", "structural_pymupdf", "layered_200dpi", "djvu", "lepton")


def _seven_zip():
    # PATH may resolve an ancient bundled 7z without LZMA2 support.
    installed = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / '7-Zip/7z.exe'
    return str(installed) if installed.is_file() else (shutil.which('7z') or shutil.which('7zz'))


def _downsample_images(doc, target=200, threshold=220):
    """Explicit image replacement, retaining existing page content and text layers.

    Use the least DPI over all occurrences so shared images are not undersampled
    at larger placements. This experimental derivative uses JPEG, including for
    bitonal originals, and can increase storage; measured results decide usefulness.
    """
    import math
    import pymupdf as fitz
    placements = {}
    for page in doc:
        for item in page.get_image_info(xrefs=True):
            ref = item.get('xref', 0)
            if not ref:
                continue
            a, b, c, d, _, _ = item['transform']
            w, h = math.hypot(a, b), math.hypot(c, d)
            if not w or not h:
                continue
            dpi = min(item['width'] * 72 / w, item['height'] * 72 / h)
            if ref not in placements or dpi < placements[ref][0]:
                placements[ref] = (dpi, page.number)
    changed = 0
    for ref, (dpi, number) in placements.items():
        if dpi <= threshold:
            continue
        pix = fitz.Pixmap(doc, ref)
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        scaled = fitz.Pixmap(pix, max(1, round(pix.width * target / dpi)),
                             max(1, round(pix.height * target / dpi)), None)
        doc[number].replace_image(ref, stream=scaled.tobytes('jpeg', jpg_quality=75))
        changed += 1
    return changed

def _tool_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name in ("7z", "7zz", "lepton", "c44", "cjb2", "ddjvu"):
        found = _seven_zip() if name == '7z' else shutil.which(name)
        entry: dict[str, Any] = {"path": found}
        if found:
            try:
                p = subprocess.run([found, "i" if name in ('7z', '7zz') else "--version"], capture_output=True, text=True, timeout=10)
                lines = (p.stdout or p.stderr).strip().splitlines()
                entry["version"] = lines[0] if p.returncode == 0 and lines else None
            except Exception: entry["version"] = None
        info[name] = entry
    for name in ("zstandard", "pymupdf"):
        try:
            module = __import__(name); info[name] = {"path": getattr(module, "__file__", None), "version": getattr(module, "__version__", None)}
        except ImportError: info[name] = {"path": None, "version": None}
    return info


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _row(path: Path, method: str, original: bytes, output_dir: Path) -> dict[str, Any]:
    """Run one method for one PDF and return a serialisable result row."""
    source_hash = _sha256(original)
    stem = f"{path.stem}.{hashlib.sha1(str(path).encode()).hexdigest()[:10]}"
    output_dir = output_dir.resolve()
    artifact_dir = output_dir / "benchmark_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "pdf": str(path), "method": method, "original_bytes": len(original),
        "compressed_bytes": None, "saving_percent": None, "encode_seconds": None,
        "decode_seconds": None, "decode_open_seconds": None, "visual_validation_seconds": None,
        "visual_validation_status": None, "peak_memory_bytes": None,
        "exact_reversible": False, "original_sha256": source_hash,
        "reconstructed_sha256": None, "visual_only": method in {"djvu"},
        "derived": method in {"structural_pymupdf", "layered_200dpi", "djvu"},
        "lossy": method in {"layered_200dpi", "djvu"},
        "preview_normalized_mae_mean": None,
        "downsampled_image_count": None,
        "status": "ok", "error": "",
    }
    if method == "djvu":
        row.update(status="skipped", error="optional DjVu adapter unavailable")
        return row
    if method == "lepton":
        found = shutil.which("lepton")
        row.update(status="skipped", error="executable available but no safe adapter implemented" if found else "lepton executable unavailable", visual_only=False, derived=False)
        return row

    artifact: Path | None = None
    reconstructed: Path | None = None
    try:
        start = time.perf_counter()
        if method == "original":
            encoded = original
            row["encode_seconds"] = time.perf_counter() - start
            row["compressed_bytes"] = len(encoded)
            row["reconstructed_sha256"] = source_hash
            row["decode_seconds"] = 0.0
        elif method == "gzip9":
            encoded = gzip.compress(original, compresslevel=9, mtime=0)
            row["encode_seconds"] = time.perf_counter() - start
            artifact = artifact_dir / f"{stem}.pdf.gz"
            artifact.write_bytes(encoded)
            row["compressed_bytes"] = len(encoded)
            start = time.perf_counter(); decoded = gzip.decompress(artifact.read_bytes())
            row["decode_seconds"] = time.perf_counter() - start
            row["reconstructed_sha256"] = _sha256(decoded)
        elif method == "zstd9":
            try:
                import zstandard as zstd
            except ImportError as exc:
                row.update(status="skipped", error=f"zstandard unavailable: {exc}")
                return row
            encoded = zstd.ZstdCompressor(level=9).compress(original)
            row["encode_seconds"] = time.perf_counter() - start
            artifact = artifact_dir / f"{stem}.pdf.zst"; artifact.write_bytes(encoded)
            row["compressed_bytes"] = len(encoded)
            start = time.perf_counter(); decoded = zstd.ZstdDecompressor().decompress(artifact.read_bytes())
            row["decode_seconds"] = time.perf_counter() - start
            row["reconstructed_sha256"] = _sha256(decoded)
        elif method == "7z_lzma2":
            seven = _seven_zip()
            if not seven:
                row.update(status="skipped", error="7z/7zz executable unavailable")
                return row
            with tempfile.TemporaryDirectory(prefix="corpus-indomicus-7z-") as td:
                src = Path(td) / path.name; src.write_bytes(original)
                archive = Path(td) / "result.7z"
                proc = subprocess.run([seven, "a", "-t7z", "-mx=9", "-m0=lzma2", str(archive), src.name], cwd=td,
                                      capture_output=True, text=True, timeout=300)
                if proc.returncode:
                    raise RuntimeError(proc.stderr.strip() or f"7z exit {proc.returncode}")
                row["encode_seconds"] = time.perf_counter() - start
                artifact = artifact_dir / f"{stem}.7z"; shutil.copyfile(archive, artifact)
                row["compressed_bytes"] = artifact.stat().st_size
                extract = Path(td) / "extract"; extract.mkdir()
                start = time.perf_counter()
                proc = subprocess.run([seven, "x", "-y", f"-o{extract}", str(artifact)], capture_output=True, text=True, timeout=300)
                if proc.returncode: raise RuntimeError(proc.stderr.strip() or f"7z exit {proc.returncode}")
                row["decode_seconds"] = time.perf_counter() - start
                decoded = (extract / path.name).read_bytes()
                row["reconstructed_sha256"] = _sha256(decoded)
        elif method in {"structural_pymupdf", "layered_200dpi"}:
            try:
                import pymupdf as fitz
            except ImportError as exc:
                row.update(status="skipped", error=f"PyMuPDF unavailable: {exc}")
                return row
            artifact = artifact_dir / f"{stem}.{method}.pdf"
            with tempfile.TemporaryDirectory(prefix="corpus-indomicus-pdf-") as td:
                source = Path(td) / path.name; source.write_bytes(original)
                with fitz.open(str(source)) as doc:
                    if method == 'layered_200dpi':
                        row['downsampled_image_count'] = _downsample_images(doc)
                    doc.save(str(artifact), garbage=4, deflate=True, clean=True)
            row["encode_seconds"] = time.perf_counter() - start
            row["compressed_bytes"] = artifact.stat().st_size
            start = time.perf_counter(); check = fitz.open(str(artifact)); check.page_count; check.close()
            row["decode_open_seconds"] = time.perf_counter() - start
            row["decode_seconds"] = row["decode_open_seconds"]
            start = time.perf_counter(); original_doc = fitz.open(stream=original, filetype="pdf"); derived_doc = fitz.open(str(artifact))
            text_ok = original_doc.page_count == derived_doc.page_count; render_ok = text_ok
            differences = []
            if text_ok:
                for i in range(original_doc.page_count):
                    text_ok = text_ok and original_doc[i].get_text() == derived_doc[i].get_text()
                    if method == 'layered_200dpi':
                        a = original_doc[i].get_pixmap(dpi=36, colorspace=fitz.csGRAY)
                        b = derived_doc[i].get_pixmap(dpi=36, colorspace=fitz.csGRAY)
                        if (a.width, a.height) != (b.width, b.height):
                            render_ok = False
                        else:
                            differences.append(sum(abs(x-y) for x,y in zip(a.samples, b.samples)) / (255 * len(a.samples)))
                        if i in {0, original_doc.page_count // 2, original_doc.page_count - 1}:
                            preview = output_dir / 'derived_preview'
                            preview.mkdir(exist_ok=True)
                            for label, document in [('original', original_doc), ('derived', derived_doc)]:
                                document[i].get_pixmap(dpi=96).save(preview / f'{path.stem}-{i+1}-{label}.png')
                    else:
                        a = original_doc[i].get_pixmap(dpi=96).tobytes("png"); b = derived_doc[i].get_pixmap(dpi=96).tobytes("png")
                        render_ok = render_ok and _sha256(a) == _sha256(b)
            original_doc.close(); derived_doc.close()
            row["visual_validation_seconds"] = time.perf_counter() - start
            row["visual_validation_status"] = "passed" if text_ok and render_ok else ("text_mismatch" if not text_ok else "render_mismatch")
            if method == 'layered_200dpi' and text_ok and render_ok:
                row['visual_validation_status'] = 'all_pages_rendered_text_preserved_quality_not_approved'
                row['preview_normalized_mae_mean'] = mean(differences) if differences else None
            if not text_ok or not render_ok:
                row.update(status="error", error="derived output failed render/text comparison")
            row["reconstructed_sha256"] = _sha256(artifact.read_bytes())
        else:
            row.update(status="skipped", error="unknown method")
            return row
        row["exact_reversible"] = row["reconstructed_sha256"] == source_hash
        if method in {"original", "gzip9", "zstd9", "7z_lzma2"} and not row["exact_reversible"]:
            row.update(status="error", error="reconstructed SHA-256 does not match original")
        if row["compressed_bytes"] is not None:
            row["saving_percent"] = (1 - row["compressed_bytes"] / len(original)) * 100 if original else 0.0
    except Exception as exc:  # benchmark should continue across problematic PDFs
        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return row


def _quantiles(values: list[float]) -> dict[str, float | None]:
    if not values: return {"p10": None, "p50": None, "p90": None}
    ordered = sorted(values)
    def q(p: float) -> float:
        pos = (len(ordered) - 1) * p; lo = int(pos); hi = min(len(ordered) - 1, lo + 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    return {"p10": q(.10), "p50": q(.50), "p90": q(.90)}


def run_benchmarks(paths: list[Path], output_dir: Path) -> dict[str, Any]:
    """Benchmark compression methods and persist ``benchmark.csv`` and JSON summary."""
    output_dir = Path(output_dir).resolve(); output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        original = path.read_bytes()
        for method in _METHODS:
            print(f'Benchmark {path.name}: {method}', flush=True)
            rows.append(_row(path, method, original, output_dir))
    fields = list(rows[0]) if rows else ["pdf", "method"]
    with (output_dir / "benchmark.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    methods: dict[str, Any] = {}
    for method in _METHODS:
        subset = [r for r in rows if r["method"] == method and r["status"] == "ok" and r["saving_percent"] is not None]
        savings = [float(r["saving_percent"]) for r in subset]
        total_original = sum(int(r["original_bytes"]) for r in subset)
        total_compressed = sum(int(r["compressed_bytes"]) for r in subset)
        methods[method] = {
            "count": len(subset), "errors": sum(r["status"] == "error" for r in rows if r["method"] == method),
            "skipped": sum(r["status"] == "skipped" for r in rows if r["method"] == method),
            "worst": min(savings) if savings else None, "mean": mean(savings) if savings else None,
            "median": median(savings) if savings else None, "best": max(savings) if savings else None,
            **_quantiles(savings), "byte_weighted_saving_percent": (1 - total_compressed / total_original) * 100 if total_original else None,
            "original_bytes": total_original, "compressed_bytes": total_compressed,
            "encode_seconds": sum(r['encode_seconds'] or 0 for r in subset),
            "decode_seconds": sum(r['decode_seconds'] or 0 for r in subset),
            "exact_count": sum(r['exact_reversible'] for r in subset),
        }
    summary = {"pdf_count": len({r["pdf"] for r in rows}), "methods": methods, "rows": len(rows), "generated_at_epoch": time.time(), "tools": _tool_info(), "settings": {"methods": list(_METHODS), "gzip_level": 9, "zstd_level": 9, "seven_zip_level": 9, "render_dpi": 96, "derived_image_dpi_threshold": 220, "derived_image_dpi_target": 200, "derived_jpeg_quality": 75, "derived_validation_gray_dpi": 36}}
    (output_dir / "benchmark_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {"rows": rows, "summary": summary}
