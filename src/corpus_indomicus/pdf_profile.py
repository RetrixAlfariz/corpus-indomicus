"""Read-only PDF census. Run with ``uv run --extra profile python -m ...``."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import re
import statistics
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf as fitz


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def quantiles(values):
    values = sorted(values)
    if not values:
        return {k: None for k in ('min', 'mean', 'p10', 'p50', 'p90', 'max')}
    def percentile(p):
        pos = (len(values) - 1) * p
        lo = int(pos)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (pos - lo)
    return dict(min=values[0], mean=statistics.mean(values), p10=percentile(.1),
                p50=percentile(.5), p90=percentile(.9), max=values[-1])


def union_area(rectangles):
    """Exact union of axis-aligned rectangles (bounding-box approximation for rotated images)."""
    xs = sorted({x for r in rectangles for x in (r[0], r[2])})
    area = 0.
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((r[1], r[3]) for r in rectangles if r[0] < right and r[2] > left)
        end = float('-inf')
        height = 0.
        for bottom, top in intervals:
            height += max(0., top - max(end, bottom))
            end = max(end, top)
        area += height * (right - left)
    return area


def classify(coverage, characters, invisible):
    if coverage >= .75:
        return 'raster+OCR' if characters >= 20 else 'raster-backed'
    if coverage >= .10:
        return 'mixed'
    return 'born-digital'


def write_csv(path, rows, empty_fields):
    fields = list(dict.fromkeys(k for r in rows for k in r)) or empty_fields
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


def write_parquet(path, rows, empty_schema):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    table = pa.Table.from_pylist([{k: row.get(k) for k in fields} for row in rows]) if rows else pa.Table.from_pylist([], schema=empty_schema)
    pq.write_table(table, path, compression='zstd')


def profile_one(path: Path, root: Path):
    data = path.read_bytes()
    digest = sha(data)
    base = dict(pdf_id=str(path.relative_to(root)), original_sha256=digest, file_bytes=len(data))
    pages, objects, occurrences = [], [], []
    with fitz.open(stream=data, filetype='pdf') as doc:
        if doc.needs_pass:
            raise ValueError('encrypted PDF requires password; not profiled')
        font_refs, font_programs, content_refs = set(), set(), set()
        font_details = {}
        usage = defaultdict(set)
        for page in doc:
            page_no = page.number + 1
            content_refs.update(page.get_contents())
            for ref in page.get_contents():
                usage[ref].add(page_no)
            for font in page.get_fonts(full=True):
                ref, ext, kind, name = font[:4]
                font_refs.add(ref)
                usage[ref].add(page_no)
                font_details[ref] = dict(font_name=name, font_embedded=False, font_subset=bool(re.match(r'^[A-Z]{6}\+', name)),
                                         font_type=kind, font_extension=ext)
                # Follow font descriptors and descendant fonts, not arbitrary page resources.
                todo, seen = [ref], set()
                while todo:
                    current = todo.pop()
                    if current in seen or current <= 0:
                        continue
                    seen.add(current)
                    for key in ('FontDescriptor', 'DescendantFonts', 'FontFile', 'FontFile2', 'FontFile3'):
                        _, val = doc.xref_get_key(current, key)
                        refs = [int(v) for v in re.findall(r'(\d+)\s+\d+\s+R', val)]
                        if key.startswith('FontFile'):
                            font_programs.update(refs)
                            if refs:
                                font_details[ref]['font_embedded'] = True
                            for program in refs:
                                font_details[program] = font_details[ref]
                                usage[program].add(page_no)
                        todo.extend(refs)
            text = page.get_text('text')
            traces = page.get_texttrace()
            invisible = sum(len(t.get('chars', ())) for t in traces if t.get('type') == 3 or t.get('opacity', 1) == 0)
            total_trace = sum(len(t.get('chars', ())) for t in traces)
            width, height = page.rect.width, page.rect.height
            # get_image_info uses unrotated page coordinates. Compare with unrotated visible bounds.
            bounds = page.rect * page.derotation_matrix
            rectangles, infos = [], page.get_image_info(hashes=True, xrefs=True)
            for ordinal, info in enumerate(infos):
                rect = fitz.Rect(info['bbox']) & bounds
                if not rect.is_empty:
                    rectangles.append(tuple(rect))
                ref = info.get('xref', 0)
                if ref:
                    usage[ref].add(page_no)
                matrix = info['transform']
                rendered_w = (matrix[0] ** 2 + matrix[1] ** 2) ** .5
                rendered_h = (matrix[2] ** 2 + matrix[3] ** 2) ** .5
                occurrences.append(dict(pdf_id=base['pdf_id'], page=page_no, image_index=ordinal,
                    xref=ref, inline_or_unresolved=not bool(ref), width_px=info['width'], height_px=info['height'],
                    colorspace=info.get('cs-name'), bits_per_component=info.get('bpc'),
                    dpi_x=info['width'] * 72 / rendered_w if rendered_w else None,
                    dpi_y=info['height'] * 72 / rendered_h if rendered_h else None,
                    bbox=json.dumps(info['bbox']), bbox_page_coverage=rect.get_area() / bounds.get_area(),
                    coverage_is_bbox_approximation=bool(matrix[1] or matrix[2]),
                    decoded_pixel_md5=info['digest'].hex()))
            coverage = min(1., union_area(rectangles) / bounds.get_area())
            drawings = page.get_drawings()
            segments = sum(len(d.get('items', ())) for d in drawings)
            classification = classify(coverage, len(text.strip()), invisible)
            pages.append(dict(pdf_id=base['pdf_id'], page=page_no, width_pt=width, height_pt=height,
                file_bytes_per_page=len(data) / len(doc), extracted_text_chars=len(text),
                image_count=len(infos), unique_image_xrefs=len({i['xref'] for i in infos if i.get('xref')}),
                image_coverage=coverage, coverage_method='clipped_bbox_union', classification=classification,
                invisible_text_chars=invisible, invisible_text_ratio=invisible / total_trace if total_trace else 0.,
                ocr_overlay_indication=coverage >= .75 and len(text.strip()) >= 20,
                ocr_overlay_confidence='strong_invisible_text' if coverage >= .75 and invisible >= 20 else 'heuristic_only',
                full_page_raster=any(r.get_area() / bounds.get_area() >= .9 for r in map(fitz.Rect, rectangles)),
                drawing_paths=len(drawings), vector_segments=segments, vector_heavy=segments >= 500,
                large_map_or_table_candidate=(max(width, height) > 1000 or segments >= 500 or
                    any(max(i['width'], i['height']) >= 4000 for i in infos)),
                candidate_is_semantically_verified=False))
        category_bytes = Counter()
        stream_total = 0
        for ref in range(1, doc.xref_length()):
            try:
                dictionary = doc.xref_object(ref, compressed=False)
                if not dictionary or dictionary == 'null':
                    continue
                stream = doc.xref_stream_raw(ref) if doc.xref_is_stream(ref) else None
                key = lambda name: doc.xref_get_key(ref, name)[1]
                subtype, kind = key('Subtype'), key('Type')
                category = ('image' if subtype == '/Image' else
                    'fonts' if ref in font_programs or ref in font_refs else
                    'content_streams' if ref in content_refs or subtype == '/Form' else
                    'metadata' if kind == '/Metadata' else
                    'embedded_files' if kind == '/EmbeddedFile' else 'other_pdf_objects')
                length = len(stream) if stream is not None else 0
                category_bytes[category] += length
                stream_total += length
                filters = re.findall(r'/([A-Za-z0-9]+)', key('Filter'))
                decoded_hash, decode_error = None, None
                # Decode font programs only: raster decompression is unnecessary for exact dedup.
                if ref in font_programs:
                    try:
                        decoded_hash = sha(doc.xref_stream(ref))
                    except Exception as exc:
                        decode_error = str(exc)
                row = dict(pdf_id=base['pdf_id'], xref=ref, category=category, is_stream=stream is not None,
                    encoded_stream_bytes=length, encoded_stream_sha256=sha(stream) if stream is not None else None,
                    normalized_dictionary_sha256=sha(dictionary.encode()),
                    normalized_dictionary_utf8_bytes=len(dictionary.encode()),
                    filters=json.dumps(filters), primary_codec=next((f for f in reversed(filters) if f in
                        ('DCTDecode', 'JPXDecode', 'FlateDecode', 'JBIG2Decode', 'CCITTFaxDecode', 'LZWDecode', 'RunLengthDecode')),
                        filters[-1] if filters else 'unfiltered'),
                    width_px=key('Width') if category == 'image' else None,
                    height_px=key('Height') if category == 'image' else None,
                    colorspace=key('ColorSpace') if category == 'image' else None,
                    bits_per_component=key('BitsPerComponent') if category == 'image' else None,
                    is_font_program=ref in font_programs, embedded_font=ref in font_programs,
                    decoded_font_sha256=decoded_hash, decode_error=decode_error,
                    used_on_pages=json.dumps(sorted(usage[ref])), **font_details.get(ref, {}))
                objects.append(row)
            except Exception as exc:
                objects.append(dict(pdf_id=base['pdf_id'], xref=ref, category='error', error=str(exc)))
        # An active xref census is not a byte-offset parser: retain residual separately.
        residual = len(data) - stream_total
        allocation = defaultdict(float)
        unassigned = residual
        for obj in objects:
            length = obj.get('encoded_stream_bytes', 0)
            refs = usage[obj['xref']]
            if refs:
                for number in refs:
                    allocation[number] += length / len(refs)
            else:
                unassigned += length
        for page in pages:
            page['allocated_bytes_estimate'] = allocation[page['page']] + unassigned / len(doc)
        kinds = {p['classification'] for p in pages}
        row = dict(**base, page_count=len(doc), mean_file_bytes_per_page=len(data) / len(doc) if len(doc) else None,
                   classification=next(iter(kinds)) if len(kinds) == 1 else 'mixed',
                   active_encoded_stream_bytes=stream_total, structural_or_unaccounted_bytes=residual,
                   stream_accounting_valid=residual >= 0, repaired=doc.is_repaired,
                   encrypted=bool(doc.metadata.get('encryption')), status='ok',
                   unresolved_xref_count=sum(o['category'] == 'error' for o in objects),
                   **{f'{k}_stream_bytes': category_bytes[k] for k in
                      ('image', 'fonts', 'content_streams', 'metadata', 'embedded_files', 'other_pdf_objects')})
        return row, pages, objects, occurrences


def duplicate_summary(objects):
    groups = defaultdict(list)
    for row in objects:
        if row.get('encoded_stream_bytes', 0):
            groups[row['encoded_stream_sha256']].append(row)
    total = sum(r.get('encoded_stream_bytes', 0) for r in objects)
    count = sum(len(g) for g in groups.values())
    duplicate_bytes = sum((len(g) - 1) * g[0]['encoded_stream_bytes'] for g in groups.values())
    cross_bytes = sum((len({r['pdf_id'] for r in g}) - 1) * g[0]['encoded_stream_bytes'] for g in groups.values())
    cross_objects = sum(len({r['pdf_id'] for r in g}) - 1 for g in groups.values())
    return dict(scope='active encoded streams only; dictionary and reference identity excluded',
                stream_bytes=total, duplicate_stream_bytes=duplicate_bytes,
                duplicate_stream_byte_ratio=duplicate_bytes / total if total else None,
                cross_document_duplicate_stream_bytes=cross_bytes,
                cross_document_ratio_definition='sum(distinct documents per stream hash - 1) / count(nonempty active streams)',
                cross_document_duplicate_object_ratio=cross_objects / count if count else None)


def run(root: Path, output: Path, benchmark=False):
    root, output = root.resolve(), output.resolve()
    if root == output or root.is_relative_to(output):
        raise ValueError('output must not contain the input root')
    output.mkdir(parents=True, exist_ok=True)
    paths = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'
                   and not p.is_relative_to(output))
    pdfs, pages, objects, images, manifest = [], [], [], [], []
    started = time.time()
    for index, path in enumerate(paths, 1):
        before = sha(path.read_bytes())
        manifest.append(dict(path=str(path), sha256=before, bytes=path.stat().st_size))
        print(f'[{index}/{len(paths)}] {path.name}', flush=True)
        try:
            pdf, pp, oo, ii = profile_one(path, root)
            pdfs.append(pdf); pages.extend(pp); objects.extend(oo); images.extend(ii)
        except Exception as exc:
            pdfs.append(dict(pdf_id=str(path.relative_to(root)), original_sha256=before,
                             file_bytes=path.stat().st_size, status='error', error=str(exc)))
    write_csv(output / 'pdf_profile.csv', pdfs, ['pdf_id', 'status', 'file_bytes', 'page_count'])
    for name, rows, schema in (
        ('page_profile', pages, [('pdf_id', pa.string()), ('page', pa.int32())]),
        ('object_profile', objects, [('pdf_id', pa.string()), ('xref', pa.int32())]),
        ('image_profile', images, [('pdf_id', pa.string()), ('page', pa.int32()), ('xref', pa.int32())])):
        write_parquet(output / f'{name}.parquet', rows, pa.schema(schema))
    total_bytes = sum(r['file_bytes'] for r in pdfs)
    codecs, categories, codec_counts, all_codecs = Counter(), Counter(), Counter(), Counter()
    for obj in objects:
        categories[obj['category']] += obj.get('encoded_stream_bytes', 0)
        if obj.get('is_stream'):
            all_codecs[obj['primary_codec']] += obj['encoded_stream_bytes']
        if obj['category'] == 'image':
            codecs[obj['primary_codec']] += obj['encoded_stream_bytes']
            codec_counts[obj['primary_codec']] += 1
    fonts = defaultdict(list)
    for obj in objects:
        if obj.get('decoded_font_sha256'):
            fonts[obj['decoded_font_sha256']].append(dict(pdf_id=obj['pdf_id'], xref=obj['xref'],
                                                         font_name=obj.get('font_name')))
    duplicates = duplicate_summary(objects)
    duplicates['duplicate_byte_ratio_of_entire_corpus'] = duplicates['duplicate_stream_bytes'] / total_bytes if total_bytes else None
    unresolved = sum(o['category'] == 'error' for o in objects)
    summary = dict(status=('complete_with_errors' if any(p['status'] != 'ok' for p in pdfs) else
                          'complete_with_warnings' if unresolved else 'complete') if paths else 'blocked_no_input_pdfs', input_root=str(root),
        total_pdf=len(pdfs), successfully_profiled_pdf=sum(p['status'] == 'ok' for p in pdfs),
        failed_pdf=sum(p['status'] != 'ok' for p in pdfs), total_pages=len(pages), total_bytes=total_bytes,
        unresolved_xref_count=unresolved,
        unresolved_xref_note='May include free/deleted xref slots; not counted as observed objects. See object_profile error rows.',
        size_per_page_allocated_estimate=quantiles([p['allocated_bytes_estimate'] for p in pages]),
        size_per_page_document_mean=quantiles([p['mean_file_bytes_per_page'] for p in pdfs if p.get('page_count')]),
        pdf_class_counts=dict(Counter(p.get('classification', 'unknown') for p in pdfs)),
        page_class_counts=dict(Counter(p['classification'] for p in pages)),
        page_class_percent={k: 100 * sum(p['classification'] == k for p in pages) / len(pages) if pages else None
                            for k in ('born-digital', 'raster-backed', 'raster+OCR', 'mixed')},
        pdf_class_percent={k: 100 * sum(p.get('classification') == k for p in pdfs) / len(pdfs) if pdfs else None
                           for k in ('born-digital', 'raster-backed', 'raster+OCR', 'mixed')},
        image_codec_encoded_bytes=dict(codecs), image_codec_object_counts=dict(codec_counts),
        all_stream_primary_codec_encoded_bytes=dict(all_codecs),
        all_stream_primary_codec_share_of_corpus={k: v / total_bytes for k, v in all_codecs.items()} if total_bytes else {},
        image_codec_share_of_corpus={k: codecs[k] / total_bytes if total_bytes else None
                                    for k in ('DCTDecode', 'JPXDecode', 'FlateDecode', 'JBIG2Decode', 'CCITTFaxDecode')},
        encoded_stream_category_bytes=dict(categories),
        image_byte_share=categories['image'] / total_bytes if total_bytes else None,
        font_byte_share=categories['fonts'] / total_bytes if total_bytes else None,
        font_inventory=dict(font_objects=len([o for o in objects if o.get('font_type') and not o.get('is_font_program')]),
                            embedded_program_streams=sum(bool(o.get('is_font_program')) for o in objects),
                            subset_font_objects=sum(bool(o.get('font_subset')) and not o.get('is_font_program') for o in objects)),
        structural_or_unaccounted_bytes=sum(p.get('structural_or_unaccounted_bytes', 0) for p in pdfs),
        duplicates=duplicates, duplicate_font_programs={h: refs for h, refs in fonts.items() if len({r['pdf_id'] for r in refs}) > 1},
        profile_seconds=time.time() - started,
        limitations=[
            'Classification and map/table flags are heuristics, not manually verified ground truth.',
            'Raster+OCR means substantial raster plus existing text; invisible text is stronger evidence, not OCR provenance.',
            'Image coverage uses clipped bounding boxes; rotation, masks, clipping paths and occlusion may overestimate.',
            'Inline images are counted by page but their encoded bytes remain in content streams.',
            'Object census uses active xrefs: previous revisions, PDF syntax, dictionaries, xref and trailer remain residual.',
            'Normalized dictionary hashes are semantic diagnostics, not hashes of original serialized object bytes.',
            'Page allocation divides referenced streams among using pages, residual uniformly; it is an estimate.',
            'Image stream dedup excludes dictionary identity; only verified container reconstruction proves exactness.',
            'No re-OCR, lossy master conversion, or original file writes are performed.'])
    (output / 'input_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (output / 'storage_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if benchmark and paths:
        from .pdf_benchmark import run_benchmarks
        run_benchmarks(paths, output)
        from .pdf_dedup import run_dedup
        run_dedup(paths, output)
    elif benchmark:
        from .pdf_benchmark import run_benchmarks
        run_benchmarks([], output)
    checks = [dict(**row, after_sha256=sha(Path(row['path']).read_bytes())) for row in manifest]
    for row in checks:
        row['unchanged'] = row['sha256'] == row['after_sha256']
    (output / 'original_integrity.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
    provenance = dict(command=sys.argv, python=sys.version, platform=platform.platform(),
        packages={name: importlib.metadata.version(name) for name in ('pymupdf', 'pyarrow', 'zstandard')},
        source_sha256={p.name: sha(p.read_bytes()) for p in Path(__file__).parent.glob('pdf_*.py')})
    (output / 'run_provenance.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    if not paths:
        (output / 'STUDY_STATUS.md').write_text(
            '# Study blocked: input PDFs unavailable\n\n'
            f'Searched recursively: `{root}`. Found **0 PDF files**.\n\n'
            'These empty tables describe the current input inventory, not Indonesian legal PDF characteristics. '
            'No corpus compression experiment was run. Synthetic unit tests are excluded from these results.\n\n'
            'Raster share, dominant codec, duplicate share, realistic savings, derived savings and custom-container '
            'viability remain unknown until the downloaded dataset path is supplied.\n', encoding='utf-8')
    else:
        (output / 'STUDY_STATUS.md').write_text(
            f"# PDF storage study\n\nStatus: {summary['status']}.\n\n"
            f"PDFs: {len(pdfs)}; profiled pages: {len(pages)}; source bytes: {total_bytes}.\n\n"
            'See storage_summary.json for composition and limitations, benchmark_summary.json for generic '
            'and derived methods (when requested), and dedup_summary.json for the verified container experiment.\n',
            encoding='utf-8')
    if any(not row['unchanged'] for row in checks):
        raise RuntimeError('Input changed during study; see original_integrity.json')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('data'))
    parser.add_argument('--output', type=Path, default=Path('reports'))
    parser.add_argument('--benchmark', action='store_true')
    args = parser.parse_args()
    if not args.input.is_dir():
        parser.error('input directory does not exist')
    result = run(args.input, args.output, args.benchmark)
    print(json.dumps(result, indent=2))
    return 2 if result['status'] == 'blocked_no_input_pdfs' else 0


if __name__ == '__main__':
    raise SystemExit(main())
