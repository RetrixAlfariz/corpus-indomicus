"""Read-only encoded stream research with disk grouping and bounded trials.

Raw encoded SHA is distinct from decoded-pixel equality. No transformed PDFs
or storage engine are produced; duplicate capacity is conditional on preserving
all original syntax, dictionaries, byte positions and revision history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from .profiler import canonical, digest_file, write_json

VERSION = '2.1.1'
REF_RE = re.compile(r'(\d+)\s+\d+\s+R')
SUBSET_RE = re.compile(r'^([A-Z]{6})\+(.*)$')


def _key(doc, xref, name):
    try:
        kind, value = doc.xref_get_key(xref, name)
        return None if kind == 'null' else str(value)
    except Exception:
        return None


def _canonical_name(name):
    if not name:
        return None, None
    name = name.lstrip('/').strip()
    match = SUBSET_RE.match(name)
    return (match.group(2), match.group(1)) if match else (name, None)


def _font_payload_ref(doc, xref):
    pending, seen = [xref], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for key, kind in [('FontFile', 'Type1'), ('FontFile2', 'TrueType'), ('FontFile3', 'embedded-subtype')]:
            match = REF_RE.search(_key(doc, current, key) or '')
            if match:
                payload = int(match.group(1))
                return payload, (_key(doc, payload, 'Subtype') or kind).lstrip('/')
        for key in ['FontDescriptor', 'DescendantFonts']:
            pending.extend(int(x) for x in REF_RE.findall(_key(doc, current, key) or ''))
    return None, None


def _init_db(path):
    db = sqlite3.connect(path)
    db.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA cache_size=-32768;
    PRAGMA temp_store=FILE;
    CREATE TABLE IF NOT EXISTS context(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(sha TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS objects(file_sha TEXT,xref INTEGER,category TEXT,encoded_bytes INTEGER,encoded_sha256 TEXT,filters TEXT,PRIMARY KEY(file_sha,xref));
    CREATE TABLE IF NOT EXISTS images(file_sha TEXT,xref INTEGER,encoded_bytes INTEGER,encoded_sha256 TEXT,width INTEGER,height INTEGER,bpc INTEGER,colorspace TEXT,codec TEXT,usage_count INTEGER,page_numbers TEXT,fullscan INTEGER,resource_page_count INTEGER,ambiguous_pages INTEGER,fullscan_pages TEXT,PRIMARY KEY(file_sha,xref));
    CREATE TABLE IF NOT EXISTS fonts(file_sha TEXT,xref INTEGER,font_type TEXT,base_name TEXT,normalized_name TEXT,subset_tag TEXT,payload_xref INTEGER,payload_bytes INTEGER,payload_sha256 TEXT,payload_filters TEXT,embedded INTEGER,PRIMARY KEY(file_sha,xref));
    CREATE TABLE IF NOT EXISTS known_objects(file_sha TEXT,xref INTEGER,category TEXT,encoded_bytes INTEGER,encoded_sha256 TEXT,filters TEXT,PRIMARY KEY(file_sha,xref));
    ''')
    return db


def _load_existing_objects(path, db):
    import pyarrow.parquet as pq
    count = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=['file_sha256','xref','category','encoded_bytes','sha256','filters']):
        db.executemany('INSERT OR REPLACE INTO known_objects VALUES(?,?,?,?,?,?)',
                       [(r['file_sha256'],r['xref'],r['category'],r['encoded_bytes'] or 0,r['sha256'],json.dumps(r['filters'])) for r in batch.to_pylist()])
        count += batch.num_rows
    db.commit()
    return count


def _placement_usage(page, resources):
    """Resolve placements by unique dimensions/BPC without decoded image hashes.

    Resource aliases with identical dimensions remain explicitly ambiguous.
    Coverage is each resolved axis-aligned placement, rotation corrected.
    """
    import pymupdf
    candidates = defaultdict(set)
    for im in resources:
        if im[0] > 0:
            candidates[(im[2],im[3],im[4])].add(im[0])
    placed, full, ambiguous = defaultdict(int), set(), set()
    for info in page.get_image_info(hashes=False, xrefs=False):
        refs = candidates.get((info['width'],info['height'],info['bpc']), set())
        if len(refs) != 1:
            ambiguous.update(refs)
            continue
        xref = next(iter(refs))
        placed[xref] += 1
        box = (pymupdf.Rect(info['bbox']) * page.rotation_matrix) & page.rect
        area = max(0,box.width) * max(0,box.height)
        if page.rect.width * page.rect.height and area / (page.rect.width * page.rect.height) >= .70:
            full.add(xref)
    return placed, full, ambiguous


def _profile_pdf(entry, db, max_file_bytes):
    import pymupdf
    path, sha = Path(entry['path']), entry['sha256']
    if path.stat().st_size != entry['logical_size'] or path.stat().st_size > max_file_bytes or digest_file(path) != sha:
        raise ValueError('source size/checksum/cap mismatch before profiling')
    known = {r[0]:r for r in db.execute('SELECT xref,category,encoded_bytes,encoded_sha256,filters FROM known_objects WHERE file_sha=?',(sha,))}
    with pymupdf.open(path) as doc:
        infos, usage, resource_pages, full_pages, ambiguous = {}, defaultdict(list), defaultdict(set), defaultdict(list), defaultdict(set)
        font_refs = set()
        for number,page in enumerate(doc,1):
            resources = list(page.get_images(full=True))
            for im in resources:
                if im[0] > 0:
                    infos.setdefault(im[0],im); resource_pages[im[0]].add(number)
            placed,full,uncertain = _placement_usage(page,resources)
            for xref,count in placed.items():
                usage[xref].extend([number]*count)
            for xref in full: full_pages[xref].append(number)
            for xref in uncertain: ambiguous[xref].add(number)
            font_refs.update(int(f[0]) for f in page.get_fonts(full=True) if f[0] > 0)
        objects,images,fonts = [],[],[]
        for xref in range(1,doc.xref_length()):
            old = known.get(xref)
            subtype = (_key(doc,xref,'Subtype') or '').lstrip('/')
            typ = (_key(doc,xref,'Type') or '').lstrip('/')
            image = xref in infos or subtype == 'Image'
            font = xref in font_refs or typ == 'Font'
            raw = doc.xref_stream_raw(xref) if doc.xref_is_stream(xref) else None
            if raw is not None:
                size,digest = len(raw),hashlib.sha256(raw).hexdigest()
                if old and (size != old[2] or digest != old[3]):
                    raise ValueError(f'encoded stream differs from validated Phase 2 xref {xref}')
                filters = re.findall(r'/([A-Za-z][A-Za-z0-9]*)',_key(doc,xref,'Filter') or '')
                category = old[1] if old else ('image' if image else 'other')
                objects.append((sha,xref,category,size,digest,json.dumps(filters)))
            else:
                size,digest,filters = 0,None,[]
                if old and old[2]:
                    raise ValueError(f'previously readable stream unavailable at xref {xref}')
            if image:
                im = infos.get(xref)
                def number(index,key):
                    value = im[index] if im else _key(doc,xref,key)
                    try: return int(value)
                    except (ValueError,TypeError): return None
                images.append((sha,xref,size,digest,number(2,'Width'),number(3,'Height'),number(4,'BitsPerComponent'),
                               im[5] if im else (_key(doc,xref,'ColorSpace') or ''),filters[-1] if filters else 'unfiltered',
                               len(usage[xref]),json.dumps(usage[xref]),bool(full_pages[xref]),len(resource_pages[xref]),
                               len(ambiguous[xref]),json.dumps(full_pages[xref])))
            if font:
                base = _key(doc,xref,'BaseFont') or _key(doc,xref,'Name')
                normalized,subset = _canonical_name(base)
                payload,kind = _font_payload_ref(doc,xref)
                record = known.get(payload)
                payload_bytes,payload_sha,payload_filters = (record[2],record[3],record[4]) if record else (0,None,'[]')
                if payload and record is None:
                    raw_font = doc.xref_stream_raw(payload)
                    payload_bytes = len(raw_font); payload_sha = hashlib.sha256(raw_font).hexdigest()
                    payload_filters = json.dumps(re.findall(r'/([A-Za-z][A-Za-z0-9]*)',_key(doc,payload,'Filter') or ''))
                fonts.append((sha,xref,kind or 'unembedded',(base or '').lstrip('/'),normalized,subset,payload,payload_bytes,payload_sha,payload_filters,bool(payload)))
        with db:
            db.executemany('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?)',objects)
            db.executemany('INSERT OR REPLACE INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',images)
            db.executemany('INSERT OR REPLACE INTO fonts VALUES(?,?,?,?,?,?,?,?,?,?,?)',fonts)
    if digest_file(path) != sha or path.stat().st_size != entry['logical_size']:
        raise ValueError('source changed during profiling')
    return {'sha256':sha,'status':'ok','images':len(images),'fonts':len(fonts),'streams':len(objects)}


def _dedup_summary(db, output=None):
    result = {'hash_basis':'SHA-256 of encoded raw stream bytes; decoded pixel/font hashes are not calculated','categories':{}}
    db.executescript('CREATE INDEX IF NOT EXISTS stream_hash ON objects(category,encoded_sha256,file_sha); CREATE INDEX IF NOT EXISTS font_names ON fonts(normalized_name,payload_sha256);')
    refs = (Path(output)/'duplicate_stream_references.jsonl').open('w',encoding='utf-8',newline='\n') if output else None
    try:
        for category in ['image','font','content','other']:
            total = db.execute('SELECT COALESCE(SUM(encoded_bytes),0) FROM objects WHERE category=?',(category,)).fetchone()[0]
            groups,intra,cross,reference_bytes,occurrences = 0,0,0,0,0
            for digest,size,count,docs in db.execute('SELECT encoded_sha256,MAX(encoded_bytes),COUNT(*),COUNT(DISTINCT file_sha) FROM objects WHERE category=? AND encoded_bytes>0 GROUP BY encoded_sha256 HAVING COUNT(*)>1',(category,)):
                groups += 1
                intra += size*(count-docs); cross += size*(docs-1)
                first = True
                for sha,xref in db.execute('SELECT file_sha,xref FROM objects WHERE category=? AND encoded_sha256=? ORDER BY file_sha,xref',(category,digest)):
                    if first: first=False; continue
                    line = canonical({'document_sha256':sha,'xref':xref,'encoded_sha256':digest,'bytes':size})+'\n'
                    reference_bytes += len(line.encode('utf-8')); occurrences += 1
                    if refs: refs.write(line)
            result['categories'][category] = {'encoded_bytes':total,'exact_duplicate_groups':groups,'intra_duplicate_payload_bytes':intra,'cross_duplicate_payload_bytes':cross,'gross_repeated_payload_bytes':intra+cross,'duplicate_occurrences':occurrences,'serialized_reference_bytes':reference_bytes,'capacity_less_reference_bytes':intra+cross-reference_bytes,'interpretation':'conditional raw-payload capacity minus actual JSONL reference bytes; full PDF reconstruction mappings/index/syntax costs unmeasured, not net storage savings'}
    finally:
        if refs: refs.close()
    result['font_nonidentical_same_name_candidates'] = [dict(normalized_name=n,distinct_encoded_hashes=h,resource_rows=c,candidate_basis='same normalized name only; not semantic or byte equality') for n,h,c in db.execute('SELECT normalized_name,COUNT(DISTINCT payload_sha256),COUNT(*) FROM fonts WHERE normalized_name IS NOT NULL AND payload_sha256 IS NOT NULL GROUP BY normalized_name HAVING COUNT(DISTINCT payload_sha256)>1')]
    result['font_unique_payload_xrefs'] = db.execute('SELECT COUNT(*) FROM objects WHERE category="font" AND encoded_bytes>0').fetchone()[0]
    return result


def _bounded_trials(db, entries):
    import pymupdf
    import zstandard
    paths = {e['sha256']:e['path'] for e in entries}
    trials = []
    for (codec,) in db.execute('SELECT DISTINCT codec FROM images ORDER BY codec'):
        seen = set()
        for sha,xref,digest,size in db.execute('SELECT file_sha,xref,encoded_sha256,encoded_bytes FROM images WHERE codec=? AND encoded_bytes BETWEEN 1 AND ? ORDER BY file_sha,xref',(codec,16*1024**2)):
            if digest in seen: continue
            with pymupdf.open(paths[sha]) as doc:
                raw = doc.xref_stream_raw(xref)
            assert hashlib.sha256(raw).hexdigest() == digest
            started = time.perf_counter(); packed = zstandard.ZstdCompressor(level=9).compress(raw)
            encode = time.perf_counter()-started
            started = time.perf_counter(); restored = zstandard.ZstdDecompressor().decompress(packed,max_output_size=16*1024**2)
            decode = time.perf_counter()-started
            exact = hashlib.sha256(restored).hexdigest() == digest and restored == raw
            trials.append(dict(codec=codec,streams=1,document_sha256=sha,xref=xref,input_bytes=size,output_bytes=len(packed),ratio=len(packed)/size,encode_seconds=encode,decode_seconds=decode,verified_sha256=exact))
            seen.add(digest)
            if len(seen)>=3: break
    similarity = []
    # Prefix comparison is intentionally bounded and performed on encoded bytes.
    for (name,) in db.execute('SELECT normalized_name FROM fonts WHERE payload_sha256 IS NOT NULL AND normalized_name IS NOT NULL GROUP BY normalized_name HAVING COUNT(DISTINCT payload_sha256)>1 ORDER BY SUM(payload_bytes) DESC,normalized_name LIMIT 10'):
        pair = list(db.execute('SELECT MIN(file_sha),payload_xref,payload_sha256,MAX(payload_bytes) FROM fonts WHERE normalized_name=? AND payload_sha256 IS NOT NULL GROUP BY payload_sha256 ORDER BY payload_sha256 LIMIT 2',(name,)))
        # Select an actual row, rather than associating an aggregate MIN with another xref.
        payloads=[]
        identities=[]
        for _,_,digest,_ in pair:
            sha,xref,size=db.execute('SELECT file_sha,payload_xref,payload_bytes FROM fonts WHERE normalized_name=? AND payload_sha256=? ORDER BY file_sha,xref LIMIT 1',(name,digest)).fetchone()
            if size>1024**2: break
            with pymupdf.open(paths[sha]) as doc: raw=doc.xref_stream_raw(xref)
            if hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('font trial checksum mismatch')
            payloads.append(raw);identities.append({'document_sha256':sha,'xref':xref,'encoded_sha256':digest,'bytes':size})
        if len(payloads)!=2: continue
        chunks=[{hashlib.sha256(raw[i:i+1024]).hexdigest() for i in range(0,len(raw),1024)} for raw in payloads]
        union=chunks[0]|chunks[1]
        similarity.append({'normalized_name':name,'members':identities,'size_ratio':min(map(len,payloads))/max(map(len,payloads)),'encoded_1kib_chunk_jaccard':len(chunks[0]&chunks[1])/len(union) if union else 1,'basis':'aligned 1KiB encoded payload chunks; not decoded glyph/semantic similarity; compression obscures structural similarity'})
    return trials,similarity


def run(manifest_path, characterization, output, *, limit=None, max_file_bytes=1024**3):
    manifest=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    identities=[{'sha256':f['sha256'],'bytes':f['logical_size'],'sources':f['sources']} for f in sorted(manifest['files'],key=lambda x:x['sha256'])]
    if hashlib.sha256(canonical(identities).encode()).hexdigest()!=manifest['manifest_sha256']: raise ValueError('manifest fingerprint mismatch')
    if limit is not None and limit<1: raise ValueError('limit must be positive')
    entries=manifest['files'][:limit] if limit else manifest['files']
    output,characterization=Path(output),Path(characterization)
    output.mkdir(parents=True,exist_ok=True)
    prior=json.loads((characterization/'dataset_manifest.json').read_text(encoding='utf-8'))
    if prior['manifest_sha256']!=manifest['manifest_sha256']: raise ValueError('characterization manifest mismatch')
    db=_init_db(output/'storage-feasibility.sqlite')
    identity=canonical({'manifest':manifest['manifest_sha256'],'version':VERSION})
    old=db.execute('SELECT value FROM context WHERE key="identity"').fetchone()
    if old and old[0]!=identity: raise ValueError('output identity/version mismatch')
    db.execute('INSERT OR IGNORE INTO context VALUES("identity",?)',(identity,));db.commit()
    known=db.execute('SELECT COUNT(*) FROM known_objects').fetchone()[0]
    if not known: known=_load_existing_objects(characterization/'object_profile.parquet',db)
    started=time.perf_counter()
    for index,entry in enumerate(entries,1):
        sha=entry['sha256']
        previous=db.execute('SELECT value FROM files WHERE sha=?',(sha,)).fetchone()
        if previous and json.loads(previous[0])['status']=='ok':
            if digest_file(Path(entry['path']))!=sha or Path(entry['path']).stat().st_size!=entry['logical_size']: raise ValueError('resumed source changed')
            continue
        try: row=_profile_pdf(entry,db,max_file_bytes)
        except Exception as exc: row={'sha256':sha,'status':'failed','error':f'{type(exc).__name__}: {exc}'}
        with db: db.execute('INSERT OR REPLACE INTO files VALUES(?,?)',(sha,canonical(row)))
        if index%25==0:
            write_json(output/'stream_progress.json',{'profiled':index,'target':len(entries),'elapsed_seconds':time.perf_counter()-started})
            print(f'streams {index}/{len(entries)}',flush=True)
    selected={e['sha256'] for e in entries}
    rows=[json.loads(v) for sha,v in db.execute('SELECT sha,value FROM files') if sha in selected]
    result=_dedup_summary(db,output)
    trials,similarity=_bounded_trials(db,manifest['files'])
    result.update(manifest_sha256=manifest['manifest_sha256'],profiler_version=VERSION,target=len(entries),profiled=sum(r['status']=='ok' for r in rows),failed=sum(r['status']!='ok' for r in rows),failures=[r for r in rows if r['status']!='ok'],reused_object_rows=known,elapsed_seconds=time.perf_counter()-started,stream_wrapping_trials=trials,font_similarity=similarity,
                  trial_selection='first 3 distinct encoded payloads per terminal codec in SHA/xref order, each <=16MiB; font comparison top 10 nonidentical normalized-name groups ranked by resource-reference bytes (not unique storage), each payload <=1MiB',
                  placement_basis='uniquely resolved dimensions/BPC of placement metadata; ambiguous same-dimension resources explicitly flagged; rotation-corrected individual bbox >=70% page is fullscan; inline images without xref excluded')
    result['image_usage']={'ambiguous_resource_rows':db.execute('SELECT COUNT(*) FROM images WHERE ambiguous_pages>0').fetchone()[0],'fullscan_resources':db.execute('SELECT COUNT(*) FROM images WHERE fullscan=1').fetchone()[0],'resources_without_resolved_placements':db.execute('SELECT COUNT(*) FROM images WHERE usage_count=0').fetchone()[0]}
    result['trial_coverage']={'image_payloads_excluded_by_16mib_cap':db.execute('SELECT COUNT(*) FROM images WHERE encoded_bytes>?',(16*1024**2,)).fetchone()[0],'sampled_image_streams':len(trials),'sampled_font_pairs':len(similarity),'selection_is_population_estimator':False}
    result['resource_limits']={'workers':1,'max_input_file_bytes':max_file_bytes,'sqlite_cache_target_bytes':32*1024**2,'image_trial_max_bytes':16*1024**2,'font_trial_max_bytes':1024**2,'decoded_images':False,'saved_compressed_payloads':False}
    result['codec_by_doctype']=[]
    for kind in sorted({s.get('document_type','unknown') for e in entries for s in e['sources']}):
        hashes={e['sha256'] for e in entries if any(s.get('document_type','unknown')==kind for s in e['sources'])}
        totals=defaultdict(int)
        for sha,codec,size in db.execute('SELECT file_sha,codec,SUM(encoded_bytes) FROM images GROUP BY file_sha,codec'):
            if sha in hashes: totals[codec]+=size
        result['codec_by_doctype'].extend({'doctype':kind,'codec':codec,'encoded_bytes':size} for codec,size in sorted(totals.items()))
    write_json(output/'storage-feasibility-summary.json',result);db.close()
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',required=True);p.add_argument('--characterization',required=True);p.add_argument('--output',required=True)
    p.add_argument('--limit',type=int);p.add_argument('--max-file-bytes',type=int,default=1024**3)
    a=p.parse_args(argv);r=run(a.manifest,a.characterization,a.output,limit=a.limit,max_file_bytes=a.max_file_bytes)
    print(json.dumps({k:r[k] for k in ['target','profiled','failed']}));return 1 if r['failed'] else 0


if __name__=='__main__':
    raise SystemExit(main())
