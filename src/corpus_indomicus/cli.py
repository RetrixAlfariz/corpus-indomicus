from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import shutil
import httpx

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from .acquisition import AcquisitionPipeline, StorageSafetyError
from .config import AppConfig, load_config, write_default_config
from .integrity import sha256_bytes
from .models import utc_now_iso
from .registry import Registry
from .storage import RawArchive
from .sources.base import HttpSourceClient
from .sources.jdih_bpk import JdihBpkConnector


console = Console()


def _human_bytes(value: float | int) -> str:
    n = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TiB"


def _settings(args: argparse.Namespace) -> tuple[AppConfig, Path]:
    cfg = load_config(args.config)
    data_dir = Path(args.data_dir or cfg.archive.data_dir)
    return cfg, data_dir


def _pipeline(cfg: AppConfig, data_dir: Path) -> AcquisitionPipeline:
    return AcquisitionPipeline(
        data_dir=data_dir,
        min_free_bytes=int(cfg.archive.min_free_gb * 1024**3),
    )


def _client(cfg: AppConfig) -> HttpSourceClient:
    return HttpSourceClient(
        delay=cfg.jdih_bpk.delay,
        timeout=cfg.jdih_bpk.timeout,
        max_retries=cfg.jdih_bpk.max_retries,
    )


def _scope(args: argparse.Namespace, cfg: AppConfig, *, sync: bool = False) -> tuple[int, int]:
    current = date.today().year
    if getattr(args, "all", False):
        return 1945, current
    if sync and args.from_year is None:
        start = max(cfg.archive.pilot_from_year, current - cfg.archive.sync_overlap_years)
    else:
        start = args.from_year if args.from_year is not None else cfg.archive.pilot_from_year
    end = args.to_year if args.to_year is not None else current
    if start > end:
        raise SystemExit("--from-year cannot be after --to-year")
    return int(start), int(end)


def _cmd_setup(args: argparse.Namespace) -> int:
    config_path = write_default_config(args.config, overwrite=args.force)
    cfg = load_config(config_path)
    data_dir = Path(args.data_dir or cfg.archive.data_dir)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    (data_dir / "objects").mkdir(parents=True, exist_ok=True)
    console.print(f"[green]Initialized[/green] {config_path} and {data_dir.resolve()}")
    return 0


def _catalog(connector, groups):
    requested = [g.strip() for g in groups.split(",") if g.strip()]
    if not requested or any(g not in {"pusat", "lembaga"} for g in requested):
        raise ValueError("groups must be pusat and/or lembaga")
    rows = [asdict(t) for t in connector.catalog_types(requested)]
    for row in rows:
        row["observed_at"] = row["observed_at"].isoformat() if row["observed_at"] else None
    for group in requested:
        if not any(t["group"] == group for t in rows):
            raise ValueError(f"catalog missing category {group}")
    return rows


def _selected_scope(args, cfg, connector, *, sync=False):
    catalog = _catalog(connector, args.groups)
    selected_ids = set(args.types.split(",")) if args.types else None
    selected = [t for t in catalog if t["classification"] in {"pusat", "lembaga"}
                and (selected_ids is None or t["type_id"] in selected_ids)]
    if selected_ids and selected_ids - {t["type_id"] for t in selected}:
        raise ValueError("type IDs not verified in requested categories")
    if not selected:
        raise ValueError("no verified target types")
    all_years = args.all_years or getattr(args, "all", False) or (
        not sync and args.from_year is None and args.to_year is None)
    if all_years and (args.from_year is not None or args.to_year is not None):
        raise ValueError("all-years and year-range cannot be combined")
    start, end = (None, None) if all_years else _scope(args, cfg, sync=sync)
    return {"groups": sorted({t["group"] for t in selected}), "types": selected, "catalog": catalog,
            "all_years": all_years, "from_year": start, "to_year": end, "query": args.query}


def _cmd_catalog(args):
    cfg, _ = _settings(args)
    with _client(cfg) as http:
        rows = _catalog(JdihBpkConnector(http), args.groups)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _cmd_plan(args):
    cfg, data_dir = _settings(args)
    with _client(cfg) as http:
        scope = _selected_scope(args, cfg, JdihBpkConnector(http))
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    print(json.dumps({"scope": scope, "strategy": "pagination-per-type",
        "partitions": len(scope["types"]) * (1 if scope["all_years"] else scope["to_year"] - scope["from_year"] + 1),
        "source_reported_entries_reference": sum(t["reported_total"] or 0 for t in scope["types"]),
        "data_dir": str(data_dir.resolve()), "reserve_gb": cfg.archive.min_free_gb,
        "disk_free_bytes": shutil.disk_usage(data_dir).free, "existing": registry.stats(),
        "pdf_samples": registry.pdf_stats()}, ensure_ascii=False, indent=2))
    return 0


def _cmd_discover(args):
    return _run_snapshot(args, mode="backfill", discovery_only=True)


def _cmd_ingest(args):
    cfg, data_dir = _settings(args)
    pipeline = _pipeline(cfg, data_dir)
    with _client(cfg) as http:
        summary = pipeline.process_run(JdihBpkConnector(http, diagnostics_dir=data_dir / "diagnostics"),
            args.run_id, limit=args.limit, retry_failed=args.retry_failed)
    print(json.dumps(asdict(summary), ensure_ascii=False))
    counts = pipeline.registry.run_counts(args.run_id)
    return 2 if any(counts.get(s, 0) for s in ("failed", "partial", "unresolved")) else 0


def _discover_with_progress(
    pipeline: AcquisitionPipeline,
    connector: JdihBpkConnector,
    run_id: int,
    *,
    query: str | None,
    quiet: bool,
) -> None:
    if quiet:
        pipeline.discover_snapshot(connector, run_id, query=query)
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("{task.fields[detail]}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Discovering finite manifest", total=None, detail="")

        def callback(event: dict) -> None:
            reported = event.get("reported_total")
            suffix = f" / source reports {reported:,}" if reported else ""
            progress.update(
                task,
                description=f"Discovering {event['year']}",
                detail=f"page {event['page']} Ãƒâ€šÃ‚Â· {event['discovered']:,} unique{suffix}",
            )

        pipeline.discover_snapshot(connector, run_id, query=query, callback=callback)


def _process_with_progress(
    pipeline: AcquisitionPipeline,
    connector: JdihBpkConnector,
    run_id: int,
    *,
    refresh_known: bool,
    retry_failed: bool,
    quiet: bool,
):
    if quiet:
        return pipeline.process_run(
            connector,
            run_id,
            refresh_known=refresh_known,
            retry_failed=retry_failed,
        )

    total = pipeline.registry.manifest_count(run_id)
    counts = pipeline.registry.run_counts(run_id)
    completed = sum(counts.get(k, 0) for k in ("done", "skipped", "partial"))
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[current]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Archiving",
            total=total,
            completed=completed,
            current="",
        )

        def callback(event: dict) -> None:
            progress.update(
                task,
                completed=min(int(event.get("completed", 0)), total),
                current=f"source {event.get('source_id', '')}",
            )

        return pipeline.process_run(
            connector,
            run_id,
            refresh_known=refresh_known,
            retry_failed=retry_failed,
            callback=callback,
        )


def _run_snapshot(args, *, mode, sync=False, discovery_only=False):
    cfg, data_dir = _settings(args)
    pipeline = _pipeline(cfg, data_dir)
    try:
        with _client(cfg) as http:
            connector = JdihBpkConnector(http, diagnostics_dir=data_dir / "diagnostics")
            if getattr(args, "run_id", None):
                run_id = args.run_id
                run = pipeline.registry.get_run(run_id)
                if not run:
                    raise ValueError(f"unknown run {run_id}")
                # Resume an existing snapshot without re-fetching/changing its catalog.
                if args.query is not None and args.query != json.loads(run["scope_json"]).get("query"):
                    raise ValueError("changed query; create a new run")
                if args.types or args.all_years or getattr(args, "all", False) or args.from_year or args.to_year or args.groups != "pusat,lembaga":
                    raise ValueError("--run-id uses its saved scope; omit scope filters")
            else:
                scope = _selected_scope(args, cfg, connector, sync=sync)
                run_id, resumed = pipeline.registry.create_scoped_run(mode=mode, provider="jdih_bpk",
                    scope=scope, snapshot_cutoff=utc_now_iso(), resume=not args.fresh)
                console.print(f"Run #{run_id}" + (" (resumed)" if resumed else ""))
            pipeline.discover_scoped(connector, run_id, max_pages=getattr(args, "max_pages", 10000))
            run = pipeline.registry.get_run(run_id)
            if not run["frozen_at"]:
                console.print(f"Run #{run_id}: discovery incomplete; no downloads started")
                return 2
            console.print(f"Manifest #{run_id} frozen: {pipeline.registry.manifest_count(run_id)} source records")
            if not discovery_only:
                summary = pipeline.process_run(connector, run_id, refresh_known=sync, limit=args.limit)
                print(json.dumps(asdict(summary), ensure_ascii=False))
    except StorageSafetyError as exc:
        console.print(f"Paused safely: {exc}")
        return 3
    counts = pipeline.registry.run_counts(run_id)
    return 2 if any(counts.get(s, 0) for s in ("failed", "partial", "unresolved")) else 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    return _run_snapshot(args, mode="backfill", sync=False)


def _cmd_sync(args: argparse.Namespace) -> int:
    return _run_snapshot(args, mode="sync", sync=True)


def _cmd_retry(args: argparse.Namespace) -> int:
    cfg, data_dir = _settings(args)
    pipeline = _pipeline(cfg, data_dir)
    run = pipeline.registry.get_run(args.run_id) if args.run_id else pipeline.registry.latest_run()
    if not run:
        console.print("No acquisition run found.")
        return 1
    run_id = int(run["id"])
    with _client(cfg) as http:
        connector = JdihBpkConnector(http)
        summary = _process_with_progress(
            pipeline,
            connector,
            run_id,
            refresh_known=True,
            retry_failed=True,
            quiet=args.no_progress,
        )
    counts = pipeline.registry.run_counts(run_id)
    console.print(f"Retried run #{run_id}: {json.dumps(counts, ensure_ascii=False)}")
    return 2 if any(counts.get(s, 0) for s in ("failed", "partial", "unresolved")) else 0


def _cmd_status(args: argparse.Namespace) -> int:
    _, data_dir = _settings(args)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    stats = registry.stats()
    pdf = registry.pdf_stats()
    refs = registry.reference_coverage()
    run = registry.get_run(args.run_id) if args.run_id else registry.latest_run()
    if args.json or args.output or args.run_id:
        report = registry.coverage(int(run["id"]) if run else None)
        if args.output:
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    table = Table(title="Corpus Indomicus v1")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Instruments", f"{stats['instruments']:,}")
    table.add_row("Source records", f"{stats['source_records']:,}")
    table.add_row("Source observations", f"{stats['source_observations']:,}")
    table.add_row("Neutral references", f"{stats['document_references']:,}")
    table.add_row("Unique objects", f"{stats['unique_content_hashes']:,}")
    table.add_row("Logical bytes", _human_bytes(stats["logical_bytes"]))
    table.add_row("Physical bytes", _human_bytes(stats["stored_bytes"]))
    if stats["logical_bytes"]:
        saving = 1 - stats["stored_bytes"] / stats["logical_bytes"]
        table.add_row("Object compression saving", f"{saving:.1%}")
    table.add_row("Reference targets known", f"{refs['known_targets']:,}/{refs['total']:,}")
    if pdf["count"]:
        table.add_row("PDF count", f"{pdf['count']:,}")
        table.add_row("PDF mean", _human_bytes(pdf["mean_bytes"]))
        table.add_row("PDF P95", _human_bytes(pdf["p95_bytes"]))
    if run:
        counts = registry.run_counts(int(run["id"]))
        table.add_row("Latest run", f"#{run['id']} {run['mode']} Ãƒâ€šÃ‚Â· {run['status']}")
        table.add_row("Latest run progress", f"{counts.get('done',0)+counts.get('skipped',0)+counts.get('partial',0):,}/{counts.get('total',0):,}")
    console.print(table)
    return 0


def _cmd_references(args: argparse.Namespace) -> int:
    _, data_dir = _settings(args)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    console.print_json(data=registry.reference_coverage())
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    _, data_dir = _settings(args)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    total = registry.stats()["unique_content_hashes"]
    rows = registry.iter_objects_batched()
    bad = 0
    missing = 0
    with Progress(
        TextColumn("Verifying"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(), console=console
    ) as progress:
        task = progress.add_task("verify", total=total)
        for row in rows:
            path = Path(row["path"])
            if not path.exists():
                missing += 1
            else:
                try:
                    data = RawArchive.read_object(path, compression=row["compression"])
                    if sha256_bytes(data) != row["sha256"]:
                        bad += 1
                except Exception:
                    bad += 1
            progress.advance(task)
    console.print(f"Verified {total:,} object(s): bad={bad:,}, missing={missing:,}")
    return 0 if bad == 0 and missing == 0 else 2


def _cmd_bpk_discover(args: argparse.Namespace) -> int:
    cfg, _ = _settings(args)
    with _client(cfg) as client:
        connector = JdihBpkConnector(client)
        documents = _bounded_documents(connector, args)
    for document in documents[: args.limit]:
        print(json.dumps({
            "source_id": document.source_id,
            "detail_url": document.detail_url,
            "title_hint": document.title_hint,
        }, ensure_ascii=False))
    console.print(f"Discovered {min(len(documents), args.limit)} document(s).", style="dim")
    return 0


def _cmd_bpk_ingest(args: argparse.Namespace) -> int:
    cfg, data_dir = _settings(args)
    pipeline = _pipeline(cfg, data_dir)
    with _client(cfg) as client:
        connector = JdihBpkConnector(client)
        documents = _bounded_documents(connector, args)
        for d in documents:
            d.metadata["year"] = args.year
        summary = pipeline.ingest_bpk(connector, documents, limit=args.limit, force=args.force)
    console.print_json(data={field: getattr(summary, field) for field in summary.__dataclass_fields__})
    return 0 if summary.errors == 0 and summary.invalid_files == 0 and summary.unresolved_details == 0 else 2


def _bounded_documents(connector, args):
    if args.limit < 0:
        raise ValueError("limit must be nonnegative")
    if args.limit == 0:
        return []
    catalog = _catalog(connector, args.groups)
    if args.type_id and not any(t["type_id"] == args.type_id and t["classification"] in {"pusat", "lembaga"} for t in catalog):
        raise ValueError("type ID not verified in selected categories")
    documents = {}
    for typ in catalog:
        if typ["classification"] not in {"pusat", "lembaga"} or (args.type_id and typ["type_id"] != args.type_id):
            continue
        for doc in connector.discover(query=args.query, year=args.year, page=args.page, type_id=typ["type_id"]):
            doc.metadata.update({"group": typ["group"], "type_id": typ["type_id"]})
            documents.setdefault(doc.source_id, doc)
            if len(documents) >= args.limit:
                return list(documents.values())
    return list(documents.values())


def _add_scope_args(parser: argparse.ArgumentParser, *, allow_all: bool = True) -> None:
    parser.add_argument("--from-year", type=int, default=None)
    parser.add_argument("--to-year", type=int, default=None)
    if allow_all:
        parser.add_argument("--all", action="store_true", help="Alias for --all-years; no historical cutoff.")
    parser.add_argument("--groups", default="pusat,lembaga")
    parser.add_argument("--types", default=None, help="Comma-separated type IDs, verified against the catalog")
    parser.add_argument("--all-years", action="store_true", help="No year filter: all source history")
    parser.add_argument("--query", default=None)
    parser.add_argument("--fresh", action="store_true", help="Start a new snapshot instead of resuming.")
    parser.add_argument("--no-progress", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-indomicus",
        description="Finite, resumable acquisition toolkit for the Corpus Indomicus legal corpus.",
    )
    parser.add_argument("--config", default="corpus.toml")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    catalog = sub.add_parser("catalog", help="Inspect authoritative source categories/types")
    catalog_sub = catalog.add_subparsers(required=True)
    types = catalog_sub.add_parser("types")
    types.add_argument("--groups", default="pusat,lembaga")
    types.set_defaults(func=_cmd_catalog)
    discovery = sub.add_parser("discover", help="Persist discovery only; never download documents")
    _add_scope_args(discovery)
    discovery.add_argument("--run-id", type=int)
    discovery.add_argument("--max-pages", type=int, default=10000, help="Per-partition bound; remains incomplete on limit")
    discovery.set_defaults(func=_cmd_discover)
    ingestion = sub.add_parser("ingest", help="Download from a frozen existing manifest")
    ingestion.add_argument("--run-id", type=int, required=True)
    ingestion.add_argument("--limit", type=int, default=None)
    ingestion.add_argument("--retry-failed", action="store_true")
    ingestion.set_defaults(func=_cmd_ingest)

    setup = sub.add_parser("setup", help="Create config and initialize the local corpus.")
    setup.add_argument("--force", action="store_true")
    setup.set_defaults(func=_cmd_setup)

    plan = sub.add_parser("plan", help="Preview scope, storage and existing corpus stats.")
    _add_scope_args(plan)
    plan.set_defaults(func=_cmd_plan)

    backfill = sub.add_parser("backfill", help="Freeze and ingest a finite historical snapshot.")
    _add_scope_args(backfill)
    backfill.add_argument("--limit", type=int, default=None)
    backfill.set_defaults(func=_cmd_backfill)

    sync = sub.add_parser("sync", help="Freeze a recent overlap window and detect new/changed records.")
    _add_scope_args(sync, allow_all=False)
    sync.add_argument("--limit", type=int, default=None)
    sync.set_defaults(func=_cmd_sync)

    retry = sub.add_parser("retry", help="Retry failed/partial items from a run.")
    retry.add_argument("--run-id", type=int, default=None)
    retry.add_argument("--no-progress", action="store_true")
    retry.set_defaults(func=_cmd_retry)

    status = sub.add_parser("status", help="Show corpus, storage, reference and run metrics.")
    status.add_argument("--run-id", type=int)
    status.add_argument("--json", action="store_true")
    status.add_argument("--output", help="Write coverage JSON")
    status.set_defaults(func=_cmd_status)

    refs = sub.add_parser("references", help="Show neutral reference target coverage.")
    refs.set_defaults(func=_cmd_references)

    verify = sub.add_parser("verify", help="Re-hash stored objects and report corruption/missing files.")
    verify.set_defaults(func=_cmd_verify)

    bpk = sub.add_parser("bpk", help="Low-level JDIH BPK development commands.")
    bpk_sub = bpk.add_subparsers(dest="bpk_command", required=True)
    discover = bpk_sub.add_parser("discover")
    discover.add_argument("--query", default=None)
    discover.add_argument("--year", type=int, default=None)
    discover.add_argument("--page", type=int, default=1)
    discover.add_argument("--limit", type=int, default=10)
    discover.add_argument("--groups", default="pusat,lembaga")
    discover.add_argument("--type-id")
    discover.set_defaults(func=_cmd_bpk_discover)

    ingest = bpk_sub.add_parser("ingest")
    ingest.add_argument("--query", default=None)
    ingest.add_argument("--year", type=int, required=True)
    ingest.add_argument("--page", type=int, default=1)
    ingest.add_argument("--limit", type=int, default=10)
    ingest.add_argument("--groups", default="pusat,lembaga")
    ingest.add_argument("--type-id")
    ingest.add_argument("--force", action="store_true")
    ingest.set_defaults(func=_cmd_bpk_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        console.print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
