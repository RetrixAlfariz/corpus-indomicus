"""Exact stream-addressed container experiment; PDF bytes are never reserialized."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import pymupdf
import zstandard

from .pdf_profile import quantiles, write_csv


def sha(data):
    return hashlib.sha256(data).hexdigest()


def partitions(raw):
    """Conservative exact substring locations; unmatched data remains opaque residual."""
    candidates = []
    try:
        with pymupdf.open(stream=raw, filetype='pdf') as doc:
            seen = set()
            for ref in range(1, doc.xref_length()):
                if not doc.xref_is_stream(ref):
                    continue
                stream = doc.xref_stream_raw(ref)
                if len(stream) < 256 or sha(stream) in seen:
                    continue
                seen.add(sha(stream))
                offset = 0
                while True:
                    offset = raw.find(stream, offset)
                    if offset < 0:
                        break
                    candidates.append((offset, offset + len(stream)))
                    offset += len(stream)
    except Exception:
        # Even malformed/encrypted PDFs can be preserved as an opaque exact chunk.
        pass
    cursor = 0
    for start, end in sorted(candidates, key=lambda x: (x[0], -x[1])):
        if start < cursor:
            continue
        if start > cursor:
            yield raw[cursor:start]
        yield raw[start:end]
        cursor = end
    if cursor < len(raw):
        yield raw[cursor:]


def reconstruct(manifest_path: Path, destination: Path):
    """Restore exact source bytes from the portable manifest and adjacent blobs."""
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    data = b''.join(zstandard.ZstdDecompressor().decompress(
        (manifest_path.parent / 'blobs' / f'{chunk}.zst').read_bytes()) for chunk in manifest['chunks'])
    if sha(data) != manifest['sha256']:
        raise ValueError('container reconstruction SHA-256 mismatch')
    if destination.exists():
        raise FileExistsError('refusing to overwrite an existing destination')
    destination.write_bytes(data)


def run_dedup(paths, output):
    container = output / 'dedup_container'
    blobs = container / 'blobs'
    blobs.mkdir(parents=True, exist_ok=True)
    rows, manifests, blob_sizes, users = [], [], {}, Counter()
    compressor = zstandard.ZstdCompressor(level=9)
    for index, path in enumerate(paths):
        raw = path.read_bytes()
        start = time.perf_counter()
        chunks = []
        for chunk in partitions(raw):
            digest = sha(chunk)
            chunks.append(digest)
            if digest not in blob_sizes:
                encoded = compressor.compress(chunk)
                (blobs / f'{digest}.zst').write_bytes(encoded)
                blob_sizes[digest] = len(encoded)
        manifest = dict(format='corpus-stream-cas-zstd-v1', filename=path.name,
                        original_bytes=len(raw), sha256=sha(raw), chunks=chunks)
        manifest_path = container / f'{index:07d}.json'
        manifest_path.write_text(json.dumps(manifest, separators=(',', ':')), encoding='utf-8')
        encode_seconds = time.perf_counter() - start
        users.update(set(chunks))
        start = time.perf_counter()
        # Read persisted manifest and blobs, not in-memory encoder state.
        persisted = json.loads(manifest_path.read_text(encoding='utf-8'))
        decoded = b''.join(zstandard.ZstdDecompressor().decompress((blobs / f'{h}.zst').read_bytes()) for h in persisted['chunks'])
        decode_seconds = time.perf_counter() - start
        exact = sha(decoded) == sha(raw)
        rows.append(dict(pdf=str(path), method='stream_cas_zstd9', original_bytes=len(raw),
            original_sha256=sha(raw), reconstructed_sha256=sha(decoded), exact_reversible=exact,
            status='ok' if exact else 'error', encode_seconds=encode_seconds, decode_seconds=decode_seconds,
            peak_memory_bytes=None, derived=False, manifest_bytes=manifest_path.stat().st_size))
        manifests.append((manifest, manifest_path))
    for row, (manifest, _) in zip(rows, manifests):
        row['compressed_bytes_allocated'] = row['manifest_bytes'] + sum(blob_sizes[h] / users[h] for h in set(manifest['chunks']))
        row['saving_percent'] = 100 * (1 - row['compressed_bytes_allocated'] / row['original_bytes'])
    # Only referenced blobs are counted; unreferenced leftovers from earlier runs are not part of this manifest set.
    total = sum(blob_sizes.values()) + sum(r['manifest_bytes'] for r in rows)
    source = sum(r['original_bytes'] for r in rows)
    summary = dict(method='stream_cas_zstd9', original_bytes=source, compressed_bytes=total,
        saving_percent=100 * (1 - total / source) if source else None,
        per_pdf_saving_percent=quantiles([r['saving_percent'] for r in rows]),
        errors=sum(r['status'] == 'error' for r in rows), unique_blobs=len(blob_sizes),
        manifest_bytes=sum(r['manifest_bytes'] for r in rows), encode_seconds=sum(r['encode_seconds'] for r in rows),
        decode_seconds=sum(r['decode_seconds'] for r in rows), all_exact=all(r['exact_reversible'] for r in rows),
        allocation='Shared compressed blob bytes divided equally among documents using the blob.',
        scope='Exact matched active stream payloads >=256 bytes plus residual spans; no dictionary reserialization.',
        limitations='Logical bytes include JSON manifests and zstd frames, exclude filesystem allocation/filenames. In-memory single-file processing; encoder timings depend on file order and cache. This is an experiment, not a production archival container.')
    write_csv(output / 'dedup_benchmark.csv', rows, ['pdf', 'method'])
    (output / 'dedup_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary
