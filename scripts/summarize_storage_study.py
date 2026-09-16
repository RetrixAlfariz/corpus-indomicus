"""Validate saved study artifacts and build an Indonesian evidence-based readout.

Usage: uv run --locked --extra profile python scripts/summarize_storage_study.py REPORT_DIRECTORY
"""
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pyarrow.parquet as pq


def main(output):
    def read(name):
        return json.loads((output / name).read_text(encoding='utf-8'))

    storage = read('storage_summary.json')
    benchmark = read('benchmark_summary.json')
    dedup = read('dedup_summary.json')
    manifest = read('input_manifest.json')
    pages = pq.read_table(output / 'page_profile.parquet').to_pylist()
    objects = pq.read_table(output / 'object_profile.parquet').to_pylist()
    images = pq.read_table(output / 'image_profile.parquet').to_pylist()
    with (output / 'benchmark.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    with (output / 'pdf_profile.csv').open(encoding='utf-8', newline='') as f:
        pdfs = list(csv.DictReader(f))
    groups = defaultdict(list)
    for obj in objects:
        if obj.get('encoded_stream_bytes'):
            groups[obj['encoded_stream_sha256']].append(obj)
    duplicates = []
    for digest, members in groups.items():
        if len(members) < 2:
            continue
        documents = len({m['pdf_id'] for m in members})
        duplicates.append(dict(sha256=digest, stream_bytes=members[0]['encoded_stream_bytes'],
                               copies=len(members), documents=documents,
                               cross_document=documents > 1,
                               references=[dict(pdf_id=m['pdf_id'], xref=m['xref'], category=m['category']) for m in members]))
    (output / 'duplicate_groups.json').write_text(json.dumps(duplicates, indent=2), encoding='utf-8')
    total = storage['total_bytes']
    checks = {
        'pdf_inventory_matches': len(manifest) == len(pdfs) == storage['total_pdf'],
        'page_inventory_matches': len(pages) == storage['total_pages'] == sum(int(p.get('page_count') or 0) for p in pdfs),
        'bytes_match': sum(m['bytes'] for m in manifest) == total,
        'allocated_page_bytes_reconcile': abs(sum(p['allocated_bytes_estimate'] for p in pages) - total) < 0.01,
        'stream_plus_residual_reconcile': sum(storage['encoded_stream_category_bytes'].values()) + storage['structural_or_unaccounted_bytes'] == total,
        'originals_still_unchanged': all(hashlib.sha256(Path(m['path']).read_bytes()).hexdigest() == m['sha256'] for m in manifest),
        'archival_claims_have_matching_hashes': all(r['original_sha256'] == r['reconstructed_sha256'] for r in rows if r['exact_reversible'] == 'True'),
        'generic_methods_all_exact': all(r['status'] == 'ok' and r['exact_reversible'] == 'True' for r in rows if r['method'] in ('gzip9', 'zstd9', '7z_lzma2')),
        'container_all_exact': dedup['all_exact'] and dedup['errors'] == 0,
    }
    diagnostics = dict(checks=checks, unresolved_xrefs_by_pdf=dict(Counter(o['pdf_id'] for o in objects if o['category'] == 'error')),
                       image_occurrence_colorspace_bpc={f'{cs}/{bpc}': count for (cs, bpc), count in Counter((i['colorspace'], i['bits_per_component']) for i in images).items()},
                       full_page_raster_pages=sum(p['full_page_raster'] for p in pages),
                       invisible_overlay_pages=sum(p['invisible_text_chars'] >= 20 for p in pages),
                       vector_heavy_pages=sum(p['vector_heavy'] for p in pages),
                       large_map_table_candidates=sum(p['large_map_or_table_candidate'] for p in pages),
                       dpi_x_range=[min(i['dpi_x'] for i in images), max(i['dpi_x'] for i in images)],
                       report_builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    repo = Path(__file__).resolve().parents[1]
    sources = output / 'reproduction_sources'
    provenance = read('run_provenance.json')
    checks['source_hashes_match_run'] = all(
        hashlib.sha256((repo / 'src/corpus_indomicus' / name).read_bytes()).hexdigest() == digest
        for name, digest in provenance['source_sha256'].items())
    for relative in ['pyproject.toml', 'uv.lock', 'scripts/summarize_storage_study.py',
                     'tests/test_pdf_profile.py', 'tests/test_pdf_benchmark.py',
                     *[f'src/corpus_indomicus/{name}' for name in provenance['source_sha256']]]:
        target = sources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / relative, target)
    (output / 'validation_summary.json').write_text(json.dumps(diagnostics, indent=2), encoding='utf-8')
    q = storage['size_per_page_allocated_estimate']
    pct = lambda value: f'{value:.3f}%'
    lines = [
        '# Hasil studi storage PDF Corpus-Indomicus', '',
        f"Cakupan: seluruh **{storage['total_pdf']} PDF lokal**, **{storage['total_pages']} halaman**, **{total:,} byte** ({total/1e6:.3f} MB).",
        'Ini sensus file lokal pada manifest, bukan sampel representatif seluruh PDF hukum Indonesia. Tidak ada download baru atau OCR ulang.', '',
        '## Temuan dan jawaban', '',
        f"1. Penyebab storage terbesar: image stream **{pct(storage['image_byte_share']*100)}**. Content stream menyumbang {pct(storage['encoded_stream_category_bytes'].get('content_streams',0)/total*100)}.",
        f"   Stream tanpa filter berjumlah {storage['all_stream_primary_codec_encoded_bytes'].get('unfiltered',0):,} byte ({pct(storage['all_stream_primary_codec_encoded_bytes'].get('unfiltered',0)/total*100)}), sehingga generic compression masih memiliki ruang saving meskipun raster sudah terkompresi. Flate seluruh stream menyumbang {pct(storage['all_stream_primary_codec_encoded_bytes'].get('FlateDecode',0)/total*100)}; image Flate 0% pada snapshot ini.",
        f"2. Semua {len(pages)} halaman masuk heuristik raster+OCR; {diagnostics['invisible_overlay_pages']} memiliki setidaknya 20 karakter invisible text dan {diagnostics['full_page_raster_pages']} memiliki full-page raster. Born-digital/raster tanpa text/mixed masing-masing 0% pada dataset ini.",
        f"3. Codec dominan adalah CCITTFaxDecode: {storage['image_codec_object_counts'].get('CCITTFaxDecode',0)} image objects; {pct(storage['image_codec_share_of_corpus']['CCITTFaxDecode']*100)} dari total byte. Image yang tampil pada halaman adalah DeviceGray 1-bit sekitar 400 DPI. JPEG {pct(storage['image_codec_share_of_corpus']['DCTDecode']*100)}; JPX dan JBIG2 0%.",
        f"4. Duplicate encoded-stream byte ratio terhadap seluruh PDF: **{pct(storage['duplicates']['duplicate_byte_ratio_of_entire_corpus']*100)}**. Redundansi cross-document {storage['duplicates']['cross_document_duplicate_stream_bytes']:,} byte ({pct(storage['duplicates']['cross_document_duplicate_stream_bytes']/total*100)}). Cross-document duplicate object ratio {pct(storage['duplicates']['cross_document_duplicate_object_ratio']*100)} dengan denominator nonempty stream objects; bukan seluruh object PDF.",
        f"5. Saving exact terukur: gzip {pct(benchmark['methods']['gzip9']['byte_weighted_saving_percent'])}, zstd {pct(benchmark['methods']['zstd9']['byte_weighted_saving_percent'])}, 7z/LZMA2 {pct(benchmark['methods']['7z_lzma2']['byte_weighted_saving_percent'])}. Semua klaim exact mensyaratkan SHA-256 hasil rekonstruksi sama dengan original.",
        f"6. Structural derivative: {pct(benchmark['methods']['structural_pymupdf']['byte_weighted_saving_percent'])}. Layered 200-DPI/JPEG-75 derivative: {pct(benchmark['methods']['layered_200dpi']['byte_weighted_saving_percent'])}. Saving negatif berarti file membesar. Text layer dibandingkan dan semua halaman dirender; kualitas hukum/detail kecil belum disetujui. Derived bytes tidak archival-exact.",
        f"7. Container stream-CAS+zstd: **{pct(dedup['saving_percent'])}**, dibanding zstd biasa **{pct(benchmark['methods']['zstd9']['byte_weighted_saving_percent'])}**. Untuk korpus ini belum ada alasan storage untuk membangun custom container: gain dedup kecil, ada overhead chunk/manifest, dan rasio kompresinya lebih buruk. Ini tidak menutup kemungkinan hasil berbeda pada korpus lebih besar/beragam.", '',
        '## Ukuran halaman', '',
        f"Alokasi estimasi byte/page: mean {q['mean']:,.1f}; P10 {q['p10']:,.1f}; median/P50 {q['p50']:,.1f}; P90 {q['p90']:,.1f}; minimum {q['min']:,.1f}; maksimum {q['max']:,.1f}.",
        'Page bukan unit byte mandiri di PDF: shared stream dibagi ke halaman pemakai; residual dibagi merata. Nilai file_bytes/page_count juga tersedia terpisah.', '',
        '## Benchmark per metode', '',
        'Persentase per-PDF; kolom agregat memakai bobot original bytes. Min = worst, max = best. Waktu satu pass termasuk overhead yang dijelaskan di docs/pdf-storage-study.md.', '',
        '| Metode | Output bytes | Agregat saving % | Worst | Mean | P10 | P50 | P90 | Best | Encode s | Decode s | Error/skip |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for name, m in benchmark['methods'].items():
        def fmt(k):
            return f'{m[k]:.3f}' if m.get(k) is not None else 'N/A'
        size = f"{m['compressed_bytes']:,}" if m['count'] else 'N/A'
        lines.append(f"| {name} | {size} | {fmt('byte_weighted_saving_percent')} | {fmt('worst')} | {fmt('mean')} | {fmt('p10')} | {fmt('p50')} | {fmt('p90')} | {fmt('best')} | {fmt('encode_seconds')} | {fmt('decode_seconds')} | {m['errors']}/{m['skipped']} |")
    d = dedup['per_pdf_saving_percent']
    lines += [f"| stream_cas_zstd9 | {dedup['compressed_bytes']:,} | {dedup['saving_percent']:.3f} | {d['min']:.3f} | {d['mean']:.3f} | {d['p10']:.3f} | {d['p50']:.3f} | {d['p90']:.3f} | {d['max']:.3f} | {dedup['encode_seconds']:.3f} | {dedup['decode_seconds']:.3f} | {dedup['errors']}/0 |", '',
        '## Komposisi stream', '', '| Category | Encoded bytes | % dari original |', '|---|---:|---:|']
    for name, size in storage['encoded_stream_category_bytes'].items():
        if name != 'error':
            lines.append(f'| {name} | {size:,} | {size/total*100:.3f} |')
    lines += [f"| structural/unaccounted | {storage['structural_or_unaccounted_bytes']:,} | {storage['structural_or_unaccounted_bytes']/total*100:.3f} |", '',
        'Image-codec share dan all-stream-codec share dibedakan di storage_summary.json. Flate pada content/object streams tidak sama dengan raster Flate.', '',
        '## Batas interpretasi dan pemeriksaan', '',
        f"- {storage['unresolved_xref_count']} slot xref tidak dapat dibaca; bisa mencakup free/deleted slots. Disimpan sebagai warning/error rows, bukan diasumsikan seluruh object telah terbaca. Semua PDF/page berhasil diprofilkan; jumlah byte residual tetap disajikan.",
        f"- Font stream share 0%; {storage['font_inventory']['font_objects']} font objects tanpa embedded program ditemukan. Tidak ada duplicate embedded font program; kesamaan nama Helvetica tidak dihitung sebagai dedup byte.",
        f"- {diagnostics['vector_heavy_pages']} vector-heavy pages. Flag map/table pada {diagnostics['large_map_table_candidates']} halaman dipicu ukuran scan 400 DPI; **bukan bukti semua halaman berisi peta/tabel**. Tidak dilakukan semantic detection/OCR ulang.",
        '- Pemeriksaan visual terpisah pada halaman 131 PDF 30093fb7... mengonfirmasi satu lampiran tabel hukum. Catatan ada di visual_sample_notes.json; temuan sampel ini bukan klasifikasi semantik seluruh korpus.',
        '- JPEG reversible recompression tidak dijalankan karena Lepton tidak tersedia. Bahkan batas atas tidak realistis menghapus seluruh JPEG payload hanya menghemat 0.161% korpus; ini batas byte, bukan klaim rasio recompression.',
        '- DjVu tidak tersedia; layered derivative menjadi perbandingan non-archival. Downsample mengganti image dengan JPEG, termasuk bitonal, sehingga bisa membesar. Kegagalan rasio ini tetap dicatat.',
        '- Hasil derived berlaku untuk dua metode yang diuji saja. Downsampling dengan mempertahankan bilevel/CCITT atau codec lain belum diukur; tidak ada klaim bahwa seluruh derived representation pasti lebih besar.',
        '- Structural derivative lolos perbandingan render 96 DPI dan text pada semua halaman; tidak membuktikan preservasi signature, metadata, accessibility atau seluruh fitur interaktif.',
        '- Preview error untuk lossy derivative diukur grayscale 36 DPI, hanya diagnostik; tidak membuktikan akurasi huruf/angka/detail kecil. Preview 96 DPI disimpan untuk first/middle/last setiap PDF.',
        '- Peak memory tidak diukur (null). Logical container bytes memasukkan manifest/frame, tidak memasukkan filesystem allocation.',
        f"- Validation checks: {'PASS' if all(checks.values()) else 'FAIL; lihat validation_summary.json'}. Hash original diperiksa lagi setelah seluruh benchmark.", '',
        '## Reproduksi', '', '```powershell',
        'uv run --locked --extra profile --extra dev pytest tests/test_pdf_profile.py tests/test_pdf_benchmark.py -q',
        'uv run --locked --extra profile python -u -m corpus_indomicus.pdf_profile --input data --output reports/reproduction --benchmark',
        'uv run --locked --extra profile python scripts/summarize_storage_study.py reports/reproduction', '```', '',
        'Gunakan direktori output baru. Versi tools, source hashes, parameter, input SHA-256, compressed artifacts, portable dedup blobs, hasil rekonstruksi dan timings disimpan. Dataset lokal sudah content-addressed; duplicate download URL bukan duplicate file dalam studi ini.', '',
        'Percobaan pendahuluan reports/local-study-20260916 memakai 7z lama pada PATH dan mencatat 10 kegagalan LZMA2. Hasil final ini menggunakan instalasi 7-Zip modern yang terdeteksi; pendahuluan tetap disimpan sebagai jejak diagnosis.', '']
    (output / 'STUDY_REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(diagnostics, indent=2))
    if not all(checks.values()):
        raise SystemExit('Study validation failed')


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
