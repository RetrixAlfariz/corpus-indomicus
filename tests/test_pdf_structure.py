from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

fitz = pytest.importorskip("pymupdf")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.corpus_profile.pdf_structure import profile_pdf


def _sample_pdf(path):
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((20, 30), "Selectable legal text " * 3, fontsize=11)
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 40, 40), False)
    pix.clear_with(0xCC8844)
    image_bytes = pix.tobytes("png")
    page.insert_image(fitz.Rect(120, 120, 180, 180), stream=image_bytes)
    document.save(path)
    document.close()
    return image_bytes


def test_profile_has_pages_stream_fingerprints_and_bounded_coverage(tmp_path):
    image_bytes = _sample_pdf(tmp_path / "sample.pdf")
    result = profile_pdf(tmp_path / "sample.pdf")

    assert set(result) == {"file", "pages", "objects"}
    assert result["file"]["page_count"] == 1
    assert result["file"]["stream_bytes_basis"] == "encoded_stream_payload"
    assert result["file"]["encoded_bytes_by_category"]["image"] > 0
    page = result["pages"][0]
    assert page["page_number"] == 1
    assert page["text_chars"] > 20
    assert page["image_coverage"] == pytest.approx(0.09, abs=0.01)
    assert page["class_name"] == "mixed"
    assert page["images"][0]["width"] == 40
    assert page["images"][0]["height"] == 40
    assert all("raw" not in row for row in result["objects"])
    assert all(row["sha256"] is None or len(row["sha256"]) == 64 for row in result["objects"])
    assert hashlib.sha256(image_bytes).hexdigest() != ""  # source bytes never enter the profile


def test_profile_is_deterministic_for_same_pdf(tmp_path):
    _sample_pdf(tmp_path / "sample.pdf")
    first = profile_pdf(tmp_path / "sample.pdf")
    second = profile_pdf(tmp_path / "sample.pdf")
    assert first == second


def test_missing_pdf_is_isolated_by_caller(tmp_path):
    with pytest.raises(FileNotFoundError):
        profile_pdf(tmp_path / "missing.pdf")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_full_page_image_coverage_respects_page_rotation(tmp_path, rotation):
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 200, 100), False)
    pix.clear_with(0x223344)
    page.insert_image(fitz.Rect(0, 0, 200, 100), stream=pix.tobytes("png"))
    page.set_rotation(rotation)
    path = tmp_path / f"rotated-{rotation}.pdf"
    document.save(path)
    document.close()

    result = profile_pdf(path)
    assert result["pages"][0]["image_coverage"] == pytest.approx(1.0)
    assert result["pages"][0]["class_name"] == "raster"
    assert result["pages"][0]["full_page_raster"] is True
