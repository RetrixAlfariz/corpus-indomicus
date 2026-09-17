"""Reproducible L1 runner. No downloads, OCR, or source deletion."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor
import csv
import importlib.metadata
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import core


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def distribution(values):
    if not values:
        return dict(worst=None, mean=None, p10=None, median=None, p90=None, best=None)
    s = sorted(values)
    def q(p):
        i = (len(s)-1)*p; lo = int(i); hi = min(lo+1,len(s)-1)
        return s[lo] + (s[hi]-s[lo])*(i-lo)
    return dict(worst=min(s), mean=statistics.mean(s), p10=q(.1), median=q(.5), p90=q(.9), best=max(s))


def one_job(job):
    path, out, policy, limit, protect, dpi = job
    target = out / policy / path.stem
    target.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    result = {'source_sha256':core.sha(path.read_bytes()), 'source_bytes':path.stat().st_size, 'policy':policy, 'status':'error'}
    try:
        document, assets, page_rows = core.encode_pdf(path, target, policy, limit, protect, dpi)
        result.update(pages=len(document['pages']), image_source_bytes=document['source']['source_image_bytes'])
        text = ''.join(p['text'] for p in document['pages'])
        text_only = core.zstd(text.encode())
        (target/'text-only-diagnostic.txt.zst').write_bytes(text_only)
        result['text_only_diagnostic_bytes'] = len(text_only)
        result['characters'] = len(text)
        result['representations'] = {}
        for name, channel in [('json_entries',False),('channel_pack',True),('flow_pack',True)]:
            package = target / (name+'.ildr')
            t = time.perf_counter()
            candidate = core.flow_document(document) if name=='flow_pack' else document
            metrics = core.save_package(package, candidate, assets, channel)
            metrics['serialize_seconds'] = time.perf_counter()-t
            t = time.perf_counter()
            decoded, decoded_assets = core.load_package(package)
            metrics['decode_and_verify_seconds'] = time.perf_counter()-t
            if decoded != candidate or decoded_assets != assets:
                raise ValueError('semantic package round-trip mismatch')
            metrics['text_roundtrip_pass'] = all(p['text'] == q['text'] for p,q in zip(document['pages'],decoded['pages']))
            metrics['asset_roundtrip_pass'] = True
            metrics['saving_percent'] = None if limit is not None else 100*(1-metrics['package_bytes']/path.stat().st_size)
            result['representations'][name] = metrics
        result.update(status='ok', original_integrity_pass=core.sha(path.read_bytes())==result['source_sha256'],
            asset_count=len(assets), region_count=sum(len(p['regions']) for p in document['pages']),
            table_candidate_pages=sum(r['table_candidates']>0 for r in page_rows),
            fallback_pages=sum(r['whole_page_fallback'] for r in page_rows),
            mean_retained_area_ratio=statistics.mean(r['retained_area_ratio'] for r in page_rows) if page_rows else 0,
            semantic_evidence_recall=None, ocr_accuracy=None, approved_for_deletion=False)
        sample = {1, max(1,len(document['pages'])//2),len(document['pages'])}
        if len(document['pages'])>=131: sample.add(131)
        core.render_html(document, assets, target/'fixed-preview.html', sample)
        flow = core.flow_document(document)
        core.render_flow_html(flow, assets, target/'preview.html', sample)
        core.render_flow_html(flow, assets, target/'viewer.html')
        write_json(target/'page_metrics.json', page_rows)
        write_json(target/'review_status.json', {'status':'requires_review', 'source_deletion_allowed':False,
                    'ocr_accuracy':None, 'evidence_recall':None, 'text_equals_source_extraction':True})
    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
    result['elapsed_seconds'] = time.perf_counter()-start
    write_json(target/'result.json',result)
    return result


def run(args):
    root, out = args.input.resolve(), args.output.resolve()
    if not root.is_dir():
        raise ValueError('input directory missing')
    if out == root or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError('input and output must be disjoint directories')
    if out.exists():
        raise FileExistsError('use a NEW output directory')
    paths = sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower()=='.pdf')
    out.mkdir(parents=True)
    if not paths:
        write_json(out/'summary.json', {'status':'blocked_no_input_pdfs','count':0,'saving_percent':None})
        return 2
    if len(paths) > args.max_files:
        raise ValueError(f'{len(paths)} files exceed --max-files {args.max_files}; choose an explicit bounded input')
    if sum(p.stat().st_size for p in paths) > args.max_source_mb * 1_000_000:
        raise ValueError('source byte budget exceeded')
    manifest = [{'filename':p.name,'sha256':core.sha(p.read_bytes()),'bytes':p.stat().st_size} for p in paths]
    if len({r['sha256'] for r in manifest}) != len(manifest):
        raise ValueError('duplicate exact PDF inputs; deduplicate paths first')
    if len({p.stem for p in paths})!=len(paths):
        raise ValueError('duplicate basenames; use source-hash filenames')
    write_json(out/'input_manifest.json',manifest)
    protect = json.loads(args.protect.read_text()) if args.protect else {}
    versions = {}
    for name in ['PyMuPDF','Pillow','numpy','opencv-python-headless','msgpack','zstandard']:
        try: versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name]='not installed; system fallback may apply'
    write_json(out/'provenance.json',{'python':sys.version,'platform':platform.platform(),'argv':sys.argv,
        'versions':versions,'core_sha256':core.sha(Path(core.__file__).read_bytes()),'study_sha256':core.sha(Path(__file__).read_bytes()),
        'workers':args.workers,'detection_dpi':args.detection_dpi,'page_limit':args.page_limit,
        'scope':'existing input PDFs only; no acquisition; no OCR; no source writes'})
    jobs = [(p,out,policy,args.page_limit,protect,args.detection_dpi) for policy in args.policies for p in paths]
    started=time.perf_counter()
    if args.workers == 1:
        results=[one_job(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            results=list(pool.map(one_job,jobs))
    summary={'status':'complete_requires_quality_review','input_documents':len(paths),
        'input_bytes':sum(r['bytes'] for r in manifest),'page_limit':args.page_limit,'original_hashes_unchanged':all(core.sha(p.read_bytes())==m['sha256'] for p,m in zip(paths,manifest)),
        'elapsed_seconds':time.perf_counter()-started,'methods':{},'errors':sum(r['status']!='ok' for r in results),
        'ocr_accuracy':None,'evidence_recall':None,'approved_for_source_deletion':False}
    flat=[]
    for policy in args.policies:
        subset=[r for r in results if r['policy']==policy and r['status']=='ok']
        complete=len(subset)==len(paths) and args.page_limit is None
        for method in ['json_entries','channel_pack','flow_pack']:
            total=sum(r['representations'][method]['package_bytes'] for r in subset)
            name=policy+'/'+method
            summary['methods'][name]={
                'successful_documents':len(subset),'pages':sum(r['pages'] for r in subset),'package_bytes':total,
                'asset_bytes':sum(r['representations'][method]['asset_bytes'] for r in subset),
                'metadata_bytes':sum(r['representations'][method]['metadata_zstd_bytes'] for r in subset),
                'container_bytes':sum(r['representations'][method]['container_overhead_bytes'] for r in subset),
                'aggregate_saving_percent':100*(1-total/summary['input_bytes']) if complete else None,
                'per_document_savings':distribution([r['representations'][method]['saving_percent'] for r in subset if r['representations'][method]['saving_percent'] is not None]),
                'text_roundtrip_pass':complete and all(r['representations'][method]['text_roundtrip_pass'] for r in subset),
                'asset_roundtrip_pass':complete and all(r['representations'][method]['asset_roundtrip_pass'] for r in subset),
                'table_candidate_pages':sum(r['table_candidate_pages'] for r in subset),
                'whole_page_fallback_pages':sum(r['fallback_pages'] for r in subset),
                'characters':sum(r['characters'] for r in subset)}
            for r in subset:
                flat.append({'source_sha256':r['source_sha256'],'source_bytes':r['source_bytes'],'pages':r['pages'],'method':name,**r['representations'][method]})
    first=args.policies[0]; docs=[]
    for p in paths:
        package=out/first/p.stem/'channel_pack.ildr'
        if package.exists():
            d,a=core.load_package(package); docs.append(d)
    text_bytes=[(''.join(p['text'] for p in d['pages'])).encode() for d in docs]
    summary['text_only_diagnostic']={'independent_bytes':sum(len(core.zstd(t)) for t in text_bytes),
        'shared_frame_bytes':len(core.zstd(b''.join(text_bytes))) if text_bytes else None,
        'scope':'UTF-8 text only; excludes layout, assets, provenance and search index; not L1'}
    write_json(out/'summary.json',summary);write_json(out/'document_results.json',results)
    if flat:
        with (out/'representation_benchmark.csv').open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    report=['# Corpus Indomicus L1 experiment','',f"Input: {len(paths)} PDFs, {summary['input_bytes']:,} bytes.",'',
        '**Status: requires quality review. No source deletion is authorized.**','',
        'Text equality is against the existing extraction, not independently corrected OCR. Region detection, role labels and vector rules are heuristic. Exact crop pixels do not establish complete evidence recall.','',
        '| Method | Bytes | Saving | Text roundtrip | Asset roundtrip |','|---|---:|---:|---|---|']
    for name,m in summary['methods'].items():
        saving='N/A (partial/failed)' if m['aggregate_saving_percent'] is None else f"{m['aggregate_saving_percent']:.3f}%"
        report.append(f"| {name} | {m['package_bytes']:,} | {saving} | {m['text_roundtrip_pass']} | {m['asset_roundtrip_pass']} |")
    report+=['','Canonical size includes compressed metadata, all retained image assets, package indexes and ZIP framing. Previews, source PDFs, test fixtures, duplicate candidate packages and diagnostic text-only outputs are excluded. Actual experimental workspace size is larger.','',
        'Guarded keeps detected table-grid envelopes as original crops. Vector candidate replaces long detected rules with vectors. Neither recognizes all signatures, stamps, text errors or table semantics. No confidence probability is invented.','',
        'Preserved source page identifiers are distinct from displayed pagination. Source text is not normalized or rewritten. Structure labels are candidates, not verified legal assertions.','',
        'The corpus is a local ten-file convenience set, not representative of all Indonesian legal PDFs. One-pass timing includes parsing, region analysis, serialization and validation. No peak-memory benchmark is claimed.']
    (out/'L1_STUDY_REPORT.md').write_text('\n'.join(report),encoding='utf-8')
    print(json.dumps(summary,indent=2),flush=True)
    return 1 if summary['errors'] or not summary['original_hashes_unchanged'] else 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--policies',nargs='+',choices=['guarded','vector_candidate'],default=['guarded','vector_candidate'])
    p.add_argument('--workers',type=int,choices=[1,2],default=1)
    p.add_argument('--max-files',type=int,default=20);p.add_argument('--max-source-mb',type=int,default=256)
    p.add_argument('--page-limit',type=int);p.add_argument('--detection-dpi',type=int,choices=[96,128,160],default=128)
    p.add_argument('--protect',type=Path,help='JSON: {source_sha256: {source_page: [bbox_in_points, ...]}}')
    args=p.parse_args()
    if args.page_limit is not None and args.page_limit<1: p.error('--page-limit must be >= 1')
    try:return run(args)
    except Exception as e:
        print(f'{type(e).__name__}: {e}',file=sys.stderr);return 2

if __name__=='__main__':
    raise SystemExit(main())
