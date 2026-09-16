"""Synthetic fixtures validate measurements; they are not corpus study results."""
import json
from pathlib import Path

import pytest

fitz = pytest.importorskip('pymupdf')
pytest.importorskip('pyarrow')
pytest.importorskip('zstandard')

from corpus_indomicus.pdf_profile import profile_one, run, sha, union_area
from corpus_indomicus.pdf_dedup import partitions, run_dedup, reconstruct


def fixture_pdf(path: Path):
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((20, 40), 'Born digital Indonesian law test')
    page = doc.new_page(width=200, height=200)
    pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 400, 400), False)
    pixmap.clear_with(220)
    page.insert_image(page.rect, pixmap=pixmap)
    page.insert_text((20, 40), 'Existing invisible OCR text layer', render_mode=3)
    doc.save(path)
    doc.close()


def test_page_profiles_and_allocation(tmp_path):
    path = tmp_path / 'fixture.pdf'
    fixture_pdf(path)
    before = sha(path.read_bytes())
    pdf, pages, objects, images = profile_one(path, tmp_path)
    assert pdf['page_count'] == 2
    assert pages[0]['classification'] == 'born-digital'
    assert pages[1]['classification'] == 'raster+OCR'
    assert pages[1]['invisible_text_chars'] >= 20
    assert pages[1]['image_coverage'] == pytest.approx(1)
    assert images[0]['dpi_x'] == pytest.approx(144)
    assert sum(p['allocated_bytes_estimate'] for p in pages) == pytest.approx(path.stat().st_size)
    assert sum(o.get('encoded_stream_bytes', 0) for o in objects) + pdf['structural_or_unaccounted_bytes'] == path.stat().st_size
    assert sha(path.read_bytes()) == before


def test_coverage_union_does_not_double_count():
    assert union_area([(0, 0, 10, 10), (5, 0, 15, 10)]) == 150


def test_exact_dedup_and_restore(tmp_path):
    original = tmp_path / 'original.pdf'
    fixture_pdf(original)
    raw = original.read_bytes()
    copy = tmp_path / 'copy.pdf'
    copy.write_bytes(raw)
    assert b''.join(partitions(raw)) == raw
    output = tmp_path / 'reports'
    result = run_dedup([original, copy], output)
    assert result['all_exact'] and result['errors'] == 0
    restored = tmp_path / 'restored.pdf'
    reconstruct(output / 'dedup_container' / '0000000.json', restored)
    assert restored.read_bytes() == raw
    with pytest.raises(FileExistsError):
        reconstruct(output / 'dedup_container' / '0000000.json', original)


def test_absent_corpus_explicitly_blocked(tmp_path):
    source = tmp_path / 'data'
    source.mkdir()
    output = tmp_path / 'reports'
    result = run(source, output)
    assert result['status'] == 'blocked_no_input_pdfs'
    assert result['image_byte_share'] is None
    assert json.loads((output / 'input_manifest.json').read_text()) == []


def test_profile_report_and_corrupt_pdf(tmp_path):
    import pyarrow.parquet as pq
    source = tmp_path / 'data'
    source.mkdir()
    fixture_pdf(source / 'valid.pdf')
    (source / 'broken.pdf').write_bytes(b'%PDF-not-a-real-document')
    output = tmp_path / 'reports'
    summary = run(source, output)
    assert summary['total_pdf'] == 2
    assert summary['failed_pdf'] == 1
    assert pq.read_table(output / 'page_profile.parquet').num_rows == 2
    assert all(r['unchanged'] for r in json.loads((output / 'original_integrity.json').read_text()))
