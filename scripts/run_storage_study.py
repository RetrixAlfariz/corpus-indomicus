"""Run the requested 2026 acquisition, then profile and benchmark its local PDFs."""
from datetime import datetime
import json
from pathlib import Path
import sys
import traceback

import truststore

truststore.inject_into_ssl()  # Native Windows certificate verification; never verify=False.

from corpus_indomicus.acquisition import AcquisitionPipeline
from corpus_indomicus.config import load_config
from corpus_indomicus.integrity import sha256_bytes
from corpus_indomicus.sources.base import HttpSourceClient
from corpus_indomicus.sources.jdih_bpk import JdihBpkConnector
from corpus_indomicus.storage import RawArchive


OUTPUT = Path('reports/2026-study').resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)


def state(phase, **details):
    row = dict(time=datetime.now().astimezone().isoformat(), phase=phase, **details)
    payload = json.dumps(row, ensure_ascii=False)
    print(payload, flush=True)
    temporary = OUTPUT / 'workflow_status.tmp'
    temporary.write_text(payload, encoding='utf-8')
    temporary.replace(OUTPUT / 'workflow_status.json')
    with (OUTPUT / 'workflow_events.jsonl').open('a', encoding='utf-8') as f:
        f.write(payload + '\n')


def main():
    cfg = load_config()
    data = Path(cfg.archive.data_dir)
    pipeline = AcquisitionPipeline(data_dir=data, min_free_bytes=int(cfg.archive.min_free_gb * 1024**3))
    pipeline._check_storage()
    run_id, resumed = pipeline.create_or_resume_run(
        mode='storage-study', provider='jdih_bpk', from_year=2026, to_year=2026,
        snapshot_cutoff=datetime.now().astimezone().isoformat(), resume=True)
    state('starting', run_id=run_id, resumed=resumed, year_scope=[2026, 2026])
    with HttpSourceClient(delay=cfg.jdih_bpk.delay, timeout=cfg.jdih_bpk.timeout,
                          max_retries=cfg.jdih_bpk.max_retries) as client:
        connector = JdihBpkConnector(client)
        pipeline.discover_snapshot(connector, run_id, callback=lambda event: state('discovery', event=event))
        if any(s['status'] != 'complete' for s in pipeline.registry.segments(run_id)):
            raise RuntimeError('Discovery incomplete; acquisition/benchmark deferred')
        state('manifest_frozen', run_id=run_id, records=pipeline.registry.manifest_count(run_id))
        pipeline.process_run(connector, run_id, refresh_known=True,
                             callback=lambda event: state('download', event=event))
        counts = pipeline.registry.run_counts(run_id)
        if counts.get('failed', 0) or counts.get('partial', 0):
            state('retry', run_id=run_id, counts=counts)
            pipeline.process_run(connector, run_id, refresh_known=True, retry_failed=True,
                                 callback=lambda event: state('retry_download', event=event))
    counts = pipeline.registry.run_counts(run_id)
    (OUTPUT / 'acquisition_summary.json').write_text(json.dumps(dict(run_id=run_id, counts=counts), indent=2), encoding='utf-8')
    state('verify_source_objects', run_id=run_id, counts=counts)
    bad = []
    for obj in pipeline.registry.iter_objects():
        payload = RawArchive.read_object(obj['path'], compression=obj['compression'])
        if sha256_bytes(payload) != obj['sha256']:
            bad.append(obj['path'])
    if bad:
        raise RuntimeError(f'Source object integrity failures: {bad}')
    pipeline._check_storage()
    state('profile_and_benchmark', run_id=run_id, counts=counts)
    from corpus_indomicus.pdf_profile import run
    result = run(data, OUTPUT / 'profile', benchmark=True)
    if not result['total_pdf']:
        raise RuntimeError('No PDF downloaded; no compression conclusion available')
    incomplete = any(counts.get(k, 0) for k in ('failed', 'partial', 'pending')) or result['failed_pdf']
    state('finished_with_gaps' if incomplete else 'finished', run_id=run_id, counts=counts,
          total_pdf=result['total_pdf'], total_pages=result['total_pages'])


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        state('failed', error=f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
        sys.exit(1)
