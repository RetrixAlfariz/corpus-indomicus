from pathlib import Path
import pytest

fitz = pytest.importorskip('pymupdf')
pytest.importorskip('zstandard')

from corpus_indomicus.pdf_benchmark import _row, _quantiles, run_benchmarks


def test_generic_roundtrip_and_derived_validation(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), 'Synthetic compression verification')
    raw = doc.tobytes()
    doc.close()
    path = tmp_path / 'source.pdf'
    path.write_bytes(raw)
    for method in ('original', 'gzip9', 'zstd9', 'structural_pymupdf'):
        row = _row(path, method, raw, tmp_path / 'out')
        assert row['status'] == 'ok', row
        if method == 'structural_pymupdf':
            assert row['derived']
            assert row['visual_validation_status'] == 'passed'
        else:
            assert row['exact_reversible']
            assert row['reconstructed_sha256'] == row['original_sha256']
    assert path.read_bytes() == raw


def test_quantiles_linear():
    assert _quantiles([0, 10]) == {'p10': 1, 'p50': 5, 'p90': 9}


def test_no_input_has_no_claimed_results(tmp_path):
    result = run_benchmarks([], tmp_path)
    assert result['summary']['pdf_count'] == 0
    assert all(s['count'] == 0 and s['byte_weighted_saving_percent'] is None
               for s in result['summary']['methods'].values())


def test_layered_derivative_preserves_text_but_is_not_exact(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=144, height=144)
    import random
    pix = fitz.Pixmap(fitz.csRGB, 800, 800, random.Random(42).randbytes(800 * 800 * 3), False)
    page.insert_image(page.rect, pixmap=pix)
    page.insert_text((10, 30), 'Existing OCR text', render_mode=3)
    raw = doc.tobytes()
    doc.close()
    source = tmp_path / 'source.pdf'
    source.write_bytes(raw)
    row = _row(source, 'layered_200dpi', raw, tmp_path / 'out')
    assert row['status'] == 'ok', row
    assert row['derived'] and row['lossy'] and not row['exact_reversible']
    assert row['visual_validation_status'] == 'all_pages_rendered_text_preserved_quality_not_approved'
    assert source.read_bytes() == raw
    derivative = next((tmp_path / 'out/benchmark_artifacts').glob('*.pdf'))
    with fitz.open(derivative) as result:
        assert result[0].get_image_info()[0]['width'] < 800
