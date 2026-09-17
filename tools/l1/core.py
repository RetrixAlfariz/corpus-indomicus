"""Read-only, review-gated L1 experiment. Not an archival replacement.

Preserves extracted text verbatim; extraction correctness is a separate question.
All geometry/region decisions are heuristics and never authorize source deletion.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import html
import io
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any

import cv2
import msgpack
import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw

cv2.setNumThreads(1)

VERSION = '0.2.0'
MAX_PAYLOAD = 256 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zstd(data: bytes, decode: bool = False) -> bytes:
    """Use python-zstandard, or the same system codec ABI for isolated tests."""
    try:
        import zstandard as z
        if decode:
            size = z.frame_content_size(data)
            if size < 0 or size > MAX_PAYLOAD:
                raise ValueError('invalid or excessive decompressed size')
            return z.ZstdDecompressor().decompress(data, max_output_size=MAX_PAYLOAD)
        return z.ZstdCompressor(level=9, write_content_size=True).compress(data)
    except ImportError:
        name = ctypes.util.find_library('zstd')
        if not name:
            raise RuntimeError('Install the pinned l1 requirements (zstandard missing)')
        lib = ctypes.CDLL(name)
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        cap = int(lib.ZSTD_getFrameContentSize(data, len(data))) if decode else lib.ZSTD_compressBound(len(data))
        if cap > MAX_PAYLOAD:
            raise ValueError('excessive payload')
        buf = ctypes.create_string_buffer(max(cap, 1))
        func = lib.ZSTD_decompress if decode else lib.ZSTD_compress
        func.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t] + ([] if decode else [ctypes.c_int])
        func.restype = ctypes.c_size_t
        size = func(buf, cap, data, len(data), *([] if decode else [9]))
        if lib.ZSTD_isError(size):
            raise ValueError('zstd encode/decode failed')
        return buf.raw[:size]


def stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def encode_asset(image: Image.Image) -> tuple[bytes, dict]:
    """Choose PNG or G4 TIFF by measured size; never threshold crop pixels."""
    image = image.convert('RGB') if image.mode not in ('L', 'RGB') else image
    if image.mode == 'RGB':
        a = np.asarray(image)
        if np.array_equal(a[:, :, 0], a[:, :, 1]) and np.array_equal(a[:, :, 0], a[:, :, 2]):
            image = image.convert('L')
    canonical = image.tobytes()
    pix_hash = sha(stable_json([image.mode, *image.size]) + canonical)
    a = np.asarray(image)
    bilevel = image.mode == 'L' and bool(np.all((a == 0) | (a == 255)))
    stored = image.convert('1', dither=Image.Dither.NONE) if bilevel else image
    candidates = []
    for fmt, opts in [('PNG', {'compress_level': 9}), *([('TIFF', {'compression': 'group4'})] if bilevel else [])]:
        b = io.BytesIO()
        stored.save(b, format=fmt, **opts)
        encoded = b.getvalue()
        decoded = Image.open(io.BytesIO(encoded)).convert(image.mode)
        if decoded.size != image.size or decoded.tobytes() != canonical:
            raise ValueError('asset pixel verification failed')
        candidates.append((encoded, fmt))
    encoded, fmt = min(candidates, key=lambda t: len(t[0]))
    return encoded, {'codec': fmt, 'mode': image.mode, 'width': image.width, 'height': image.height,
                     'pixel_sha256': pix_hash, 'encoded_sha256': sha(encoded), 'bytes': len(encoded)}


def rect_clip(rect, width, height):
    x0, y0, x1, y1 = rect
    return [max(0, min(width, x0)), max(0, min(height, y0)), max(0, min(width, x1)), max(0, min(height, y1))]


def intersects(a, b, padding=0):
    return a[0] <= b[2] + padding and b[0] <= a[2] + padding and a[1] <= b[3] + padding and b[1] <= a[3] + padding


def merge_rects(rects, padding=0):
    """Merge intersecting crops, preserving their entire unmodified rectangles."""
    out = []
    for rect in rects:
        rect = list(rect)
        changed = True
        while changed:
            changed = False
            kept = []
            for other in out:
                if intersects(rect, other, padding):
                    rect = [min(rect[0], other[0]), min(rect[1], other[1]), max(rect[2], other[2]), max(rect[3], other[3])]
                    changed = True
                else:
                    kept.append(other)
            out = kept
        out.append(rect)
    return out


def page_image(page, max_pixels=40_000_000):
    """Prefer unresampled full-page raster. Otherwise render at 200 DPI.

    Rotation is normalized on the in-memory Page only; no source save is allowed.
    """
    original_rotation = page.rotation
    if original_rotation:
        page.set_rotation(0)
    width, height = page.rect.width, page.rect.height
    infos = page.get_image_info(xrefs=True)
    if len(infos) == 1:
        info = infos[0]
        a, b, c, d, e, f = info['transform']
        full = max(abs(e), abs(f), abs(a - width), abs(d - height), abs(b), abs(c)) < .1
        if full and info.get('xref') and info['width'] * info['height'] <= max_pixels:
            pix = fitz.Pixmap(page.parent, info['xref'])
            if not pix.alpha and pix.n in (1, 3) and not info.get('has-mask') and not page.get_drawings() and not list(page.annots() or []):
                visible = any(t.get('type') != 3 and t.get('opacity', 1) != 0 for t in page.get_texttrace())
                if not visible:
                    return Image.frombytes('L' if pix.n == 1 else 'RGB', (pix.width, pix.height), pix.samples), 'native_raster', original_rotation
    dpi = min(200, max(1, int(72 * math.sqrt(max_pixels / (width * height)))))
    if width * height * (dpi / 72) ** 2 > max_pixels:
        raise ValueError('page exceeds raster memory budget')
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples), f'page_render_{dpi}dpi', original_rotation


def classify_line(text: str) -> tuple[str, str | None]:
    """Candidate roles, not assertions about legal hierarchy or correctness."""
    t = text.strip()
    for kind, pattern in [
        ('chapter', r'BAB\s+([IVXLCDM]+)$'),
        ('article', r'Pasal\s+([0-9]+[A-Z]?)$'),
        ('section', r'Bagian\s+(.+)$'),
        ('paragraph', r'\(([0-9]+)\)\s+'),
        ('item', r'([a-z])\.\s+'),
    ]:
        match = re.match(pattern, t)
        if match:
            return kind, match.group(1)
    if t.lower().rstrip(' :') in {'menimbang', 'mengingat', 'memutuskan', 'menetapkan'}:
        return 'preamble', None
    if t.startswith(('LAMPIRAN', 'PENJELASAN')):
        return 'appendix', None
    if re.search(r'(ditetapkan di|diundangkan di|salinan sesuai|ttd\.?$)', t, re.I):
        return 'closing', None
    return 'text', None


def extract_text(page):
    """Pool equals PyMuPDF extraction in source order, not normalized prose."""
    tp = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
    text = tp.extractText(sort=False)
    detail = tp.extractDICT(sort=False)
    lines, cursor = [], 0
    for block in detail['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            content = ''.join(span['text'] for span in line['spans'])
            if not content:
                continue
            start = text.find(content, cursor)
            if start < 0:
                continue
            if start > cursor:
                lines.append({'start': cursor, 'length': start - cursor, 'bbox': None, 'role': 'unmapped', 'label': None, 'block': -1})
            length = len(content) + int(text[start + len(content):start + len(content) + 1] == '\n')
            role, label = classify_line(content)
            lines.append({'start': start, 'length': length, 'bbox': [round(v, 2) for v in line['bbox']], 'role': role, 'label': label, 'block': block['number']})
            cursor = start + length
    if cursor < len(text):
        lines.append({'start': cursor, 'length': len(text) - cursor, 'bbox': None, 'role': 'unmapped', 'label': None, 'block': -1})
    if ''.join(text[r['start']:r['start'] + r['length']] for r in lines) != text:
        raise ValueError('text interval accounting failed')
    words = tp.extractWORDS()
    return text, lines, words


def detect_regions(image: Image.Image, page_size, words, policy='guarded', dpi=128, protected=()):
    """Heuristic non-text crops; not an evidence-recall guarantee.

    Guarded keeps ruled-table envelopes. Vector candidate replaces detected long
    straight lines but still keeps unexplained marks and manual protected regions.
    Components are expanded BEFORE cropping the untouched source pixels.
    """
    pw, ph = page_size
    dw, dh = max(1, round(pw * dpi / 72)), max(1, round(ph * dpi / 72))
    if dw * dh > 6_000_000:
        scale = math.sqrt(6_000_000 / (dw * dh)); dw, dh = max(1,int(dw*scale)), max(1,int(dh*scale))
    gray = np.asarray(image.convert('L').resize((dw, dh), Image.Resampling.LANCZOS))
    ink = (gray < 205).astype(np.uint8)
    sx, sy = dw / pw, dh / ph
    mask = np.zeros((dh, dw), np.uint8)
    for x0, y0, x1, y1, text, *_ in words:
        ambiguous_number = bool(re.fullmatch(r'[0-9OoIl.,/\-]+', text)) and any(c.isdigit() for c in text) and any(c in 'OoIl' for c in text)
        if ambiguous_number or '\ufffd' in text or any(c in text for c in '{}|'):
            continue
        box = rect_clip([math.floor(x0 * sx - 1), math.floor(y0 * sy - 1), math.ceil(x1 * sx + 1), math.ceil(y1 * sy + 1)], dw, dh)
        a, b, c, d = map(int, box)
        if d > b and c > a:
            mask[b:d, a:c] = 1
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(24, dw // 16)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(24, dh // 30), 1), np.uint8))
    vector_mask = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    vectors = []
    for orientation, part in [('h', horizontal), ('v', vertical)]:
        n, _, stats, _ = cv2.connectedComponentsWithStats(part, 8)
        for x, y, w, h, area in stats[1:]:
            if orientation == 'h':
                vectors.append([round(x / sx, 2), round((y + h / 2) / sy, 2), round((x + w) / sx, 2), round((y + h / 2) / sy, 2), round(max(.35, h / sy), 2)])
            else:
                vectors.append([round((x + w / 2) / sx, 2), round(y / sy, 2), round((x + w / 2) / sx, 2), round((y + h) / sy, 2), round(max(.35, w / sx), 2)])
    grids = []
    n, lab, stats, _ = cv2.connectedComponentsWithStats(vector_mask, 8)
    for j, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if w > dw * .22 and h > dh * .09 and np.any(horizontal[lab == j]) and np.any(vertical[lab == j]):
            grids.append([int(x), int(y), int(x + w), int(y + h)])
    # Do not mistake flat portions of curved stamps for table rules.
    table_zone = np.zeros_like(ink)
    for x0,y0,x1,y1 in grids:
        table_zone[y0:y1,x0:x1] = 1
    vector_mask &= table_zone
    vectors = [v for v in vectors if any(
        g[0]/sx-1 <= min(v[0],v[2]) and max(v[0],v[2]) <= g[2]/sx+1
        and g[1]/sy-1 <= min(v[1],v[3]) and max(v[1],v[3]) <= g[3]/sy+1 for g in grids)]
    retained = ink & (1 - mask)
    if policy == 'vector_candidate':
        retained[vector_mask != 0] = 0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    outside_labels = np.unique(labels[retained != 0])
    selected = np.zeros(n, np.uint8)
    selected[outside_labels] = 1
    selected[0] = 0
    for j, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if w > 65 * sx and h > 14 * sy and area > 60:
            if policy == 'guarded' or not np.any(vector_mask[labels == j]):
                selected[j] = 1
    if policy == 'guarded':
        retained = selected[labels]
    else:
        # Do not expand a rule-connected component into a whole table.
        rule_labels = np.unique(labels[vector_mask != 0])
        selected[rule_labels] = 0
        candidate = selected[labels]
        retained |= candidate
        retained[vector_mask != 0] = 0
    expanded = cv2.dilate(retained, np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(expanded, 8)
    rects = [[int(x), int(y), int(x + w), int(y + h)] for x, y, w, h, area in stats[1:]]
    if policy == 'guarded':
        rects.extend(grids)
    for box in protected:
        rects.append([int(box[0] * sx), int(box[1] * sy), int(math.ceil(box[2] * sx)), int(math.ceil(box[3] * sy))])
    rects = merge_rects(rects, padding=3)
    rects = [rect_clip([x0 - 3, y0 - 3, x1 + 3, y1 + 3], dw, dh) for x0, y0, x1, y1 in rects]
    rects = merge_rects(rects)
    area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    fallback = not words or len(rects) > 100 or area > dw * dh * .8
    if fallback:
        rects = [[0, 0, dw, dh]]
        area = dw * dh
        vectors = []
    boxes = [[round(x0 / sx, 3), round(y0 / sy, 3), round(x1 / sx, 3), round(y1 / sy, 3)] for x0, y0, x1, y1 in rects]
    return boxes, ([] if policy == 'guarded' else vectors), {
        'table_candidates': len(grids), 'crop_count': len(boxes), 'retained_area_ratio': area / (dw * dh),
        'whole_page_fallback': fallback, 'detection_dpi': dpi, 'evidence_recall': None,
        'interpretation': 'heuristic; automated ink coverage is not semantic evidence recall',
    }


def columnize(document):
    """Channelized metadata with one immutable text pool and shared role dictionary."""
    out = {k: v for k, v in document.items() if k != 'pages'}
    out['line_columns'] = ['start', 'length', 'bbox', 'role', 'label', 'block']
    roles = sorted({line['role'] for p in document['pages'] for line in p['lines']})
    out['roles'] = roles
    out['pages'] = []
    for page in document['pages']:
        p = {k: v for k, v in page.items() if k != 'lines'}
        p['lines'] = [[line[k] if k != 'role' else roles.index(line[k]) for line in page['lines']] for k in out['line_columns']]
        out['pages'].append(p)
    return out


def uncolumnize(document):
    out = {k: v for k, v in document.items() if k not in ('pages', 'line_columns', 'roles')}
    out['pages'] = []
    keys, roles = document['line_columns'], document['roles']
    for page in document['pages']:
        p = {k: v for k, v in page.items() if k != 'lines'}
        columns = page['lines']
        if len(columns) != len(keys) or len({len(c) for c in columns}) > 1:
            raise ValueError('invalid line channels')
        p['lines'] = []
        for row in zip(*columns):
            line = dict(zip(keys, row)); line['role'] = roles[line['role']]
            p['lines'].append(line)
        out['pages'].append(p)
    return out


def flow_document(document):
    """Remove line coordinates on non-table pages while preserving raw text."""
    import copy
    d = copy.deepcopy(document)
    d['presentation'] = 'adaptive-flow-v1'
    for p in d['pages']:
        fixed = bool(p['audit'].get('table_candidates') or p['audit'].get('whole_page_fallback'))
        p['layout_mode'] = 'fixed_table_or_fallback' if fixed else 'flow'
        if fixed:
            continue
        merged = []
        for line in p['lines']:
            line['bbox'] = None
            if merged and line['role']=='text' and merged[-1]['role']=='text' and line['block']==merged[-1]['block']:
                merged[-1]['length'] += line['length']
            else:
                merged.append(line)
        p['lines'] = merged
    return d


def render_flow_html(doc, assets, path: Path, page_numbers=None):
    """Readable reflow, plus retained visual crops. No invented OCR correction."""
    out=['<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         '<title>Corpus Indomicus L1 reading view</title><style>body{font:17px/1.7 Georgia,serif;max-width:840px;margin:32px auto;padding:0 20px;color:#1a2731;background:#faf9f5}h1,h2,summary,header{font-family:system-ui}article{background:white;border:1px solid #ddd;padding:24px;margin:24px 0}p{margin:12px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 monospace}.warning{background:#fff1d9;padding:16px;border-left:4px solid #a46612}img{max-width:100%;height:auto}figure{padding:12px;border:1px solid #ddd}figcaption{font:12px system-ui}h3{text-align:center}details{margin:20px 0}</style>',
         '<header><h1>Corpus Indomicus · L1</h1><div class="warning">Prototipe untuk pencarian dan pembacaan. Teks berasal dari layer PDF, belum dikoreksi OCR. Peran struktur dan crop visual masih memerlukan audit. Bukan salinan resmi.</div></header>']
    for p in doc['pages']:
        if page_numbers is not None and p['number'] not in page_numbers: continue
        out.append(f'<article id="page-{p["number"]}"><h2>Halaman sumber {p["number"]}</h2>')
        if p.get('layout_mode')=='fixed_table_or_fallback':
            out.append('<p><strong>Halaman bertabel/kompleks:</strong> hubungan baris dan kolom belum tervalidasi. Gunakan tampilan posisi pada fixed-preview.html atau crop tabel di bawah.</p>')
        for line in p['lines']:
            text=p['text'][line['start']:line['start']+line['length']]
            rendered=html.escape(' '.join(text.splitlines()))
            tag='h3' if line['role'] in ('chapter','article','section','appendix') else 'p'
            out.append(f'<{tag}>{rendered}</{tag}>')
        out.append('<details><summary>Crop visual yang dipertahankan</summary>')
        for i,reg in enumerate(p['regions'],1):
            uri=asset_data_uri(assets[reg['asset']])
            out.append(f'<figure><img src="{uri}" alt="Crop visual {i}"><figcaption>Crop {i}; klasifikasi belum diverifikasi. Posisi sumber: {html.escape(str(reg["bbox"]))}</figcaption></figure>')
        out.append('</details><details><summary>Teks mentah: urutan dan karakter hasil ekstraksi</summary><pre>'+html.escape(p['text'])+'</pre></details></article>')
    out.append('</html>');path.write_text('\n'.join(out),encoding='utf-8')


def validate_document(doc, assets):
    if doc.get('format') != 'indomicus-l1-experiment-v1':
        raise ValueError('unsupported format')
    for page in doc['pages']:
        if not isinstance(page['number'], int) or page['number'] < 1 or not all(isinstance(page[k], (int,float)) and math.isfinite(page[k]) and page[k] > 0 for k in ('width','height')):
            raise ValueError('invalid page geometry')
        for reg in page['regions']:
            box=reg['bbox']
            if len(box)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box) or box[2]<box[0] or box[3]<box[1]:
                raise ValueError('invalid crop geometry')
        for vector in page['vectors']:
            if len(vector)!=5 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in vector):
                raise ValueError('invalid vector')
        text = page['text']
        if sha(text.encode()) != page['text_sha256']:
            raise ValueError('text hash mismatch')
        cursor = 0
        for line in page['lines']:
            if line['start'] != cursor or line['length'] < 0:
                raise ValueError('invalid text spans')
            cursor += line['length']
            box = line['bbox']
            if box is not None and (len(box) != 4 or not all(math.isfinite(v) for v in box)):
                raise ValueError('invalid geometry')
        if cursor != len(text):
            raise ValueError('incomplete text spans')
        for region in page['regions']:
            name = region['asset']
            if name not in assets:
                raise ValueError('missing asset')
    for name, spec in doc['assets'].items():
        b = assets[name]
        if sha(b) != spec['encoded_sha256']:
            raise ValueError('asset hash mismatch')
        with Image.open(io.BytesIO(b)) as image:
            if image.size != (spec['width'], spec['height']):
                raise ValueError('asset dimensions mismatch')
            image = image.convert(spec['mode'])
            if sha(stable_json([image.mode, *image.size]) + image.tobytes()) != spec['pixel_sha256']:
                raise ValueError('asset pixel hash mismatch')


def save_package(path: Path, doc, assets, channelized=True):
    if path.exists():
        raise FileExistsError(path)
    value = columnize(doc) if channelized else doc
    packed_assets = b''
    if channelized:
        value['asset_index'] = {}
        chunks, offset = [], 0
        for name, data in sorted(assets.items()):
            value['asset_index'][name] = [offset, len(data)]
            chunks.append(data); offset += len(data)
        packed_assets = b''.join(chunks)
    raw = msgpack.packb(value, use_bin_type=True) if channelized else stable_json(value)
    payload = zstd(raw)
    manifest = {'format': 'indomicus-l1-experiment-v1', 'version': VERSION,
                'encoding': 'channel-msgpack-zstd' if channelized else 'json-zstd',
                'payload_sha256': sha(payload), 'uncompressed_bytes': len(raw), 'asset_count': len(assets)}
    entries = [('manifest.json', stable_json(manifest)), ('document.zst', payload)]
    entries += [('assets.bin', packed_assets)] if channelized else sorted(('assets/' + k, v) for k, v in assets.items())
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, data)
    return {'package_bytes': path.stat().st_size, 'metadata_zstd_bytes': len(payload),
            'asset_bytes': sum(map(len, assets.values())),
            'container_overhead_bytes': path.stat().st_size - len(payload) - sum(map(len, assets.values()))}


def load_package(path: Path):
    with zipfile.ZipFile(path) as z:
        if len(set(z.namelist())) != len(z.namelist()):
            raise ValueError('duplicate archive entries')
        if any(n.startswith('/') or '..' in Path(n).parts for n in z.namelist()):
            raise ValueError('unsafe archive path')
        if sum(i.file_size for i in z.infolist()) > MAX_PAYLOAD:
            raise ValueError('oversized package')
        m = json.loads(z.read('manifest.json'))
        if m['format'] != 'indomicus-l1-experiment-v1' or m['version'] != VERSION:
            raise ValueError('unsupported container version')
        payload = z.read('document.zst')
        if sha(payload) != m['payload_sha256']:
            raise ValueError('payload hash mismatch')
        raw = zstd(payload, True)
        if len(raw) != m['uncompressed_bytes']:
            raise ValueError('payload length mismatch')
        if m['encoding'] == 'channel-msgpack-zstd':
            value = msgpack.unpackb(raw, raw=False, strict_map_key=True)
            index = value.pop('asset_index')
            data = z.read('assets.bin'); cursor = 0; assets = {}
            for name, (offset, length) in index.items():
                if offset != cursor or length < 0 or offset + length > len(data):
                    raise ValueError('invalid asset interval')
                assets[name] = data[offset:offset + length]; cursor += length
            if cursor != len(data):
                raise ValueError('unaccounted asset bytes')
            doc = uncolumnize(value)
        elif m['encoding'] == 'json-zstd':
            doc = json.loads(raw)
            assets = {n[7:]: z.read(n) for n in z.namelist() if n.startswith('assets/')}
        else:
            raise ValueError('unsupported encoding')
        if len(assets) != m['asset_count']:
            raise ValueError('asset count mismatch')
    validate_document(doc, assets)
    return doc, assets


def asset_data_uri(b):
    im = Image.open(io.BytesIO(b)); out = io.BytesIO(); im.save(out, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def render_html(doc, assets, path: Path, page_numbers=None):
    """Offline, escaped fixed-layout review view, with selectable raw text."""
    output = ['<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Corpus Indomicus L1 review</title><style>body{font:16px system-ui;margin:24px;background:#eee;color:#17212b} header,details{max-width:850px;margin:16px auto} .page{background:white;margin:24px auto;max-width:850px;border:1px solid #aaa} svg{width:100%;display:block} pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px} .warning{border-left:4px solid #b36a00;padding:12px;background:#fff6db} text{font-family:Georgia,serif} a{color:#174e88}</style>',
        '<header><h1>Corpus Indomicus · L1 preview</h1><div class="warning">Hasil konversi eksperimental. Teks mengikuti layer ekstraksi dan dapat mengandung salah OCR. Crop dan struktur belum diaudit menyeluruh. Ini bukan salinan resmi atau pixel-exact.</div></header>']
    uris = {}
    for p in doc['pages']:
        if page_numbers is not None and p['number'] not in page_numbers:
            continue
        w, h = p['width'], p['height']
        output.append(f'<header id="page-{p["number"]}"><h2>Halaman sumber {p["number"]}</h2><p>{html.escape(p["policy"])} · {len(p["regions"])} crop · evidence: belum tervalidasi</p></header>')
        output.append(f'<section class="page"><svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">')
        for line in p['lines']:
            if line['bbox'] is None:
                continue
            a,b,c,d = line['bbox']
            text = p['text'][line['start']:line['start']+line['length']].rstrip('\n')
            if not text.strip():
                continue
            size = max(3, min(24, (d-b)*.77))
            output.append(f'<text x="{a}" y="{b+size}" font-size="{size}" textLength="{max(1,c-a)}" lengthAdjust="spacingAndGlyphs">{html.escape(text)}</text>')
        for a,b,c,d,width in p['vectors']:
            output.append(f'<line x1="{a}" y1="{b}" x2="{c}" y2="{d}" stroke="black" stroke-width="{width}"/>')
        for reg in p['regions']:
            name = reg['asset']
            if name not in uris:
                uris[name] = asset_data_uri(assets[name])
            a,b,c,d = reg['bbox']
            output.append(f'<image x="{a}" y="{b}" width="{c-a}" height="{d-b}" href="{uris[name]}" preserveAspectRatio="none"/>')
        output.append('</svg></section>')
        output.append('<details><summary>Teks selectable mentah (urutan ekstraksi sumber)</summary><pre>' + html.escape(p['text']) + '</pre></details>')
    output.append('</html>')
    path.write_text('\n'.join(output), encoding='utf-8')


def encode_pdf(path: Path, output: Path, policy='guarded', page_limit=None, protection=None, detection_dpi=128):
    """Produce a document model plus lossless crops, never write to source."""
    raw_hash = sha(path.read_bytes())
    assets, specs, pages, audits = {}, {}, [], []
    source_image_bytes = 0
    protection = protection or {}
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError('encrypted PDF; password required')
        for xref in range(1, pdf.xref_length()):
            try:
                if pdf.xref_get_key(xref, 'Subtype')[1] == '/Image' and pdf.xref_is_stream(xref):
                    source_image_bytes += len(pdf.xref_stream_raw(xref))
            except Exception:
                pass
        count = len(pdf) if page_limit is None else min(page_limit, len(pdf))
        for idx in range(count):
            page = pdf[idx]
            image, basis, rotation = page_image(page)
            text, lines, words = extract_text(page)
            pw, ph = page.rect.width, page.rect.height
            protected = list(protection.get(raw_hash, {}).get(str(idx + 1), []))
            closing = [l['bbox'][1] for l in lines if l['bbox'] and re.search(r'(salinan sesuai|ditetapkan di|diundangkan di|^ttd[. ]*$)', text[l['start']:l['start']+l['length']].strip(), re.I)]
            if closing:
                protected.append([pw*.08, max(0,min(closing)-10), pw*.98, ph*.98])
            boxes, vectors, info = detect_regions(image, (pw,ph), words, policy, detection_dpi, protected)
            regions = []
            for box in boxes:
                x0,y0,x1,y1 = box
                pixel_box = [max(0, math.floor(x0/pw*image.width)),max(0, math.floor(y0/ph*image.height)),
                             min(image.width, math.ceil(x1/pw*image.width)),min(image.height, math.ceil(y1/ph*image.height))]
                if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
                    continue
                crop = image.crop(pixel_box)
                b, spec = encode_asset(crop)
                name = spec['encoded_sha256'] + ('.tif' if spec['codec'] == 'TIFF' else '.png')
                assets[name] = b; specs[name] = spec
                exact_box = [pixel_box[0]/image.width*pw,pixel_box[1]/image.height*ph,pixel_box[2]/image.width*pw,pixel_box[3]/image.height*ph]
                regions.append({'bbox': [round(x, 4) for x in exact_box], 'asset': name, 'type': 'unclassified_visual', 'source_pixel_bbox': pixel_box})
            p = {'number': idx+1, 'width': round(pw,4), 'height': round(ph,4), 'source_rotation': rotation,
                 'text': text, 'text_sha256': sha(text.encode()), 'lines': lines, 'vectors': vectors, 'regions': regions,
                 'policy': policy, 'asset_basis': basis, 'audit': info}
            pages.append(p)
            if idx in {0,count//2,count-1,130}:
                preview = image.copy(); preview.thumbnail((900,1400))
                preview.save(output / f'source-page-{idx+1}.png')
            audits.append({'page': idx+1, 'characters':len(text), 'line_count':len(lines), 'role_candidates':sum(l['role'] != 'text' for l in lines), **info})
            if idx % 25 == 0:
                print(f'{path.stem[:8]} {policy}: page {idx+1}/{count}', flush=True)
    if sha(path.read_bytes()) != raw_hash:
        raise ValueError('source hash changed')
    document = {'format': 'indomicus-l1-experiment-v1', 'converter_version': VERSION,
        'source': {'sha256':raw_hash, 'bytes':path.stat().st_size, 'filename':path.name, 'source_image_bytes':source_image_bytes},
        'quality': {'text_baseline':'PyMuPDF selectable-text extraction, unsorted, unnormalized', 'ocr_accuracy':None,
                    'evidence_recall':None, 'semantic_structure_accuracy':None, 'approved_for_source_deletion':False,
                    'status':'requires_review', 'crop_pixel_verification':'exact relative to extraction bitmap; not entire PDF'},
        'pages':pages, 'assets':specs}
    validate_document(document, assets)
    return document, assets, audits
