"""Bounded, read-only structural characterization of a PDF.

This module deliberately reports structure and byte fingerprints only.  It does
not render pages, decode image pixels, or expose raw content streams.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

try:  # Keep import failure actionable for callers using the optional profile extra.
    import pymupdf as fitz
except ImportError:  # pragma: no cover - exercised only without the optional extra
    try:
        import fitz  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        fitz = None  # type: ignore[assignment]


_FILTER_RE = re.compile(r"/[A-Za-z][A-Za-z0-9]*")
_TEXT_PATTERN_RE = re.compile(r"\s+")
_IMAGE_COVERAGE_THRESHOLD = 0.05
_TEXT_CHAR_THRESHOLD = 20
_REF_RE = re.compile(r"(\d+)\s+0\s+R")


def _filters(doc: Any, xref: int) -> list[str]:
    try:
        kind, value = doc.xref_get_key(xref, "Filter")
    except Exception:
        return []
    if kind in {"name", "array"}:
        return [token[1:] for token in _FILTER_RE.findall(str(value))]
    return []


def _key(doc: Any, xref: int, name: str) -> str | None:
    try:
        kind, value = doc.xref_get_key(xref, name)
        return str(value) if kind != "null" else None
    except Exception:
        return None


def _stream(doc: Any, xref: int) -> bytes | None:
    try:
        return bytes(doc.xref_stream_raw(xref))
    except Exception:
        return None


def _text_pattern(text: str) -> str:
    """Return a deliberately coarse, content-free text layout fingerprint."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    shapes = [re.sub(r"\d+", "#", _TEXT_PATTERN_RE.sub(" ", line))[:80] for line in lines[:64]]
    return "|".join(shapes)


def _image_details(doc: Any, image: tuple[Any, ...]) -> dict[str, Any]:
    xref = int(image[0])
    # get_images metadata is available without asking MuPDF to decode image data.
    details: dict[str, Any] = {
        "xref": xref,
        "width": int(image[2]) if len(image) > 2 else None,
        "height": int(image[3]) if len(image) > 3 else None,
        "bpc": int(image[4]) if len(image) > 4 else None,
        "colorspace": str(image[5]) if len(image) > 5 else None,
        "filtercodec": None,
    }
    filters = _filters(doc, xref)
    if filters:
        details["filtercodec"] = filters[-1]
    return details


def _classify(text_chars: int, image_coverage: float, blocks: int) -> tuple[str, str]:
    if image_coverage >= 0.70 and text_chars >= _TEXT_CHAR_THRESHOLD:
        return "raster_text", "image-dominant page also contains selectable text; OCR is possible but unproven"
    if text_chars >= _TEXT_CHAR_THRESHOLD and image_coverage < _IMAGE_COVERAGE_THRESHOLD:
        return "born", "text-rich page with negligible image coverage"
    if image_coverage >= 0.70 and text_chars < _TEXT_CHAR_THRESHOLD:
        return "raster", "image-dominant page with little selectable text"
    if text_chars >= _TEXT_CHAR_THRESHOLD and image_coverage >= _IMAGE_COVERAGE_THRESHOLD:
        return "mixed", "selectable text and placed images both present"
    if blocks or image_coverage:
        return "unknown", "below heuristic thresholds; inspect source-specific evidence"
    return "unknown", "no measurable text blocks or image placements"


def profile_pdf(path: str | Path) -> dict[str, Any]:
    """Profile one PDF without rendering it or returning raw content.

    Exceptions are intentionally allowed to reach the caller, which can isolate
    a bad file while processing a corpus.
    """
    if fitz is None:  # pragma: no cover
        raise RuntimeError("PyMuPDF is required; install the 'profile' extra")
    pdf_path = Path(path)
    file_size = pdf_path.stat().st_size
    with pdf_path.open("rb") as stream:
        header = stream.read(32)
    version_match = re.search(rb"%PDF-(\d+\.\d+)", header)
    warnings: list[str] = []
    doc = fitz.open(pdf_path)
    try:
        xref_length = max(0, int(doc.xref_length()) - 1)
        image_xrefs: set[int] = set()
        font_xrefs: set[int] = set()
        font_program_xrefs: set[int] = set()
        content_xrefs: set[int] = set()
        pages: list[dict[str, Any]] = []
        for number, page in enumerate(doc, 1):
            rect = page.rect
            page_area = max(0.0, float(rect.width) * float(rect.height))
            images = list(page.get_images(full=True))
            page_images: list[dict[str, Any]] = []
            image_rects: list[Any] = []
            for image in images:
                xref = int(image[0])
                image_xrefs.add(xref)
                page_images.append(_image_details(doc, image))
            try:
                # Image info returns placement boxes without decoding pixel data.
                rotation_matrix = page.rotation_matrix
                image_rects = [fitz.Rect(*info["bbox"]) * rotation_matrix for info in page.get_image_info(hashes=False, xrefs=False)]
            except Exception:
                warnings.append(f"page_{number}:image_info_unavailable")
            image_rects = [
                fitz.Rect(
                    max(0.0, float(r.x0)), max(0.0, float(r.y0)),
                    min(float(rect.width), float(r.x1)), min(float(rect.height), float(r.y1)),
                )
                for r in image_rects
                if min(float(rect.width), float(r.x1)) > max(0.0, float(r.x0))
                and min(float(rect.height), float(r.y1)) > max(0.0, float(r.y0))
            ]
            union_area = 0.0
            # Union of axis-aligned rectangles via x-slab sweep; bounded by page area.
            xs = sorted({x for r in image_rects for x in (float(r.x0), float(r.x1))})
            for left, right in zip(xs, xs[1:]):
                if right <= left:
                    continue
                ys = sorted((float(r.y0), float(r.y1)) for r in image_rects if float(r.x0) < right and float(r.x1) > left)
                covered = 0.0
                end = None
                for top, bottom in ys:
                    if end is None:
                        covered, end = bottom - top, bottom
                    elif top > end:
                        covered += bottom - top
                        end = bottom
                    elif bottom > end:
                        covered += bottom - end
                        end = bottom
                union_area += (right - left) * covered
            union_area = min(max(union_area, 0.0), page_area)
            text = page.get_text("text") or ""
            text_chars = len("".join(text.split()))
            text_flags = getattr(fitz, "TEXTFLAGS_BLOCKS", 0) & ~getattr(fitz, "TEXT_PRESERVE_IMAGES", 0)
            blocks = [block for block in (page.get_text("blocks", flags=text_flags) or []) if len(block) < 7 or int(block[6]) == 0]
            try:
                fonts = list(page.get_fonts(full=True))
                font_xrefs.update(int(f[0]) for f in fonts if f and int(f[0]) > 0)
            except Exception:
                fonts = []
                warnings.append(f"page_{number}:fonts_unavailable")
            try:
                content_xrefs.update(int(x) for x in page.get_contents() or [] if int(x) > 0)
            except Exception:
                warnings.append(f"page_{number}:contents_unavailable")
            coverage = union_area / page_area if page_area else 0.0
            class_name, rationale = _classify(text_chars, coverage, len(blocks))
            pages.append({
                "page_number": number,
                "width": float(rect.width),
                "height": float(rect.height),
                "rotation": int(page.rotation),
                "text_chars": text_chars,
                "text_blocks": len(blocks),
                "text_pattern_fingerprint": hashlib.sha256(_text_pattern(text).encode()).hexdigest() if text else None,
                "image_count": len(images),
                "image_coverage": round(coverage, 8),
                "images": page_images,
                "font_count": len(fonts),
                "class_name": class_name,
                "class_rationale": rationale,
                "heuristic_uncertainty": class_name == "unknown",
                "full_page_raster": coverage >= 0.70,
            })

        objects: list[dict[str, Any]] = []
        category_bytes = {"image": 0, "font": 0, "content": 0, "other": 0}
        unresolved = 0
        for candidate in range(1, xref_length + 1):
            for key_name in ("FontFile", "FontFile2", "FontFile3"):
                value = _key(doc, candidate, key_name)
                if value:
                    match = _REF_RE.search(value)
                    if match:
                        font_program_xrefs.add(int(match.group(1)))
        for xref in range(1, xref_length + 1):
            raw = _stream(doc, xref)
            dictionary = None
            try:
                dictionary = str(doc.xref_object(xref, compressed=False)).encode("utf-8")
            except Exception:
                pass
            if raw is None:
                if dictionary is None:
                    unresolved += 1
                category = "font" if xref in font_program_xrefs else "other"
                objects.append({"xref": xref, "category": category, "encoded_bytes": 0, "sha256": None, "filters": [], "dictionary_sha256": hashlib.sha256(dictionary).hexdigest() if dictionary is not None else None})
                continue
            object_type = _key(doc, xref, "Type") or ""
            subtype = _key(doc, xref, "Subtype") or ""
            # Include unreferenced resources and font files while retaining page
            # references as the primary source of image/font classification.
            category = (
                "image" if xref in image_xrefs or subtype == "/Image"
                else "font" if xref in font_xrefs or xref in font_program_xrefs or "/Font" in object_type or any(_key(doc, xref, key) for key in ("FontFile", "FontFile2", "FontFile3"))
                else "content" if xref in content_xrefs
                else "other"
            )
            category_bytes[category] += len(raw)
            objects.append({
                "xref": xref,
                "category": category,
                "encoded_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "filters": _filters(doc, xref),
                "dictionary_sha256": hashlib.sha256(dictionary).hexdigest() if dictionary is not None else None,
            })
        trailer = ""
        try:
            trailer = str(doc.pdf_trailer())
        except Exception:
            pass
        increments = len(re.findall(r"/Prev\s+\d+", trailer))
        file_info = {
            "path": str(pdf_path),
            "bytes": file_size,
            "page_count": len(pages),
            "pdf_version": version_match.group(1).decode() if version_match else None,
            "metadata": dict(doc.metadata or {}),
            "encrypted": bool(doc.is_encrypted),
            "repaired": bool(getattr(doc, "is_repaired", False)),
            "xref_objects": xref_length,
            "xref_unresolved": unresolved,
            "increment_count_lower_bound": increments,
            "increment_count_is_lower_bound": True,
            "stream_encoded_bytes": sum(category_bytes.values()),
            "stream_bytes_basis": "encoded_stream_payload",
            "encoded_bytes_by_category": category_bytes,
            "residual_file_bytes": file_size - sum(category_bytes.values()),
            "content_stream_count": len(content_xrefs),
            "image_object_count": len(image_xrefs),
            "font_program_count": len(font_program_xrefs),
            "warnings": sorted(set(warnings)),
        }
        if file_info["residual_file_bytes"] < 0:
            file_info["warnings"].append("negative_residual_stream_accounting_overlap")
        return {"file": file_info, "pages": pages, "objects": objects}
    finally:
        doc.close()
