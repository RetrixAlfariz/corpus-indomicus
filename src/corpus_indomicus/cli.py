from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import shutil

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


def _cmd_plan(args: argparse.Namespace) -> int:
    cfg, data_dir = _settings(args)
    start, end = _scope(args, cfg)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    stats = registry.stats()
    pdf = registry.pdf_stats()
    data_dir.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(data_dir)

    table = Table(title="Corpus Indomicus v1 acquisition plan")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Mode", "finite snapshot")
    table.add_row("Source", "JDIH BPK")
    table.add_row("Year scope", f"{start} → {end}")
    table.add_row("Snapshot cutoff", "frozen when backfill starts")
    table.add_row("Existing instruments", f"{stats['instruments']:,}")
    table.add_row("Known source records", f"{stats['source_records']:,}")
    table.add_row("Physical corpus storage", _human_bytes(stats["stored_bytes"]))
    table.add_row("Disk free", _human_bytes(disk.free))
    table.add_row("Configured reserve", f"{cfg.archive.min_free_gb:.1f} GiB")
    if pdf["count"]:
        table.add_row("PDF samples", f"{pdf['count']:,}")
        table.add_row("PDF mean", _human_bytes(pdf["mean_bytes"]))
        table.add_row("PDF median", _human_bytes(pdf["median_bytes"]))
        table.add_row("PDF P95", _human_bytes(pdf["p95_bytes"]))
        table.add_row("Largest PDF", _human_bytes(pdf["max_bytes"]))
    console.print(table)
    if disk.free <= int(cfg.archive.min_free_gb * 1024**3):
        console.print(
            "[red]Storage warning:[/red] available free space is already at or below "
            "the configured safety reserve. Backfill will pause before downloading."
        )
    console.print(
        "[dim]Backfill first freezes a manifest, then downloads exactly that finite manifest. "
        "New source records are left for the next sync.[/dim]"
    )
    return 0


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
                detail=f"page {event['page']} · {event['discovered']:,} unique{suffix}",
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


def _run_snapshot(args: argparse.Namespace, *, mode: str, sync: bool = False) -> int:
    cfg, data_dir = _settings(args)
    start, end = _scope(args, cfg, sync=sync)
    cutoff = datetime.now().astimezone().isoformat(timespec="seconds")
    pipeline = _pipeline(cfg, data_dir)
    run_id, resumed = pipeline.create_or_resume_run(
        mode=mode,
        provider="jdih_bpk",
        from_year=start,
        to_year=end,
        snapshot_cutoff=cutoff,
        resume=not args.fresh,
    )
    run = pipeline.registry.get_run(run_id)
    console.print(
        f"[bold]Run #{run_id}[/bold] · {mode} · {start}→{end} · "
        f"cutoff {run['snapshot_cutoff']}" + (" [yellow](resumed)[/yellow]" if resumed else "")
    )

    try:
        with _client(cfg) as http:
            connector = JdihBpkConnector(http)
            if str(run["status"]) in {"discovering", "partial"}:
                _discover_with_progress(
                    pipeline,
                    connector,
                    run_id,
                    query=args.query,
                    quiet=args.no_progress,
                )
            total = pipeline.registry.manifest_count(run_id)
            console.print(f"Manifest frozen: [bold]{total:,}[/bold] source record(s).")
            summary = _process_with_progress(
                pipeline,
                connector,
                run_id,
                refresh_known=sync,
                retry_failed=False,
                quiet=args.no_progress,
            )
    except StorageSafetyError as exc:
        console.print(f"[red]Paused safely:[/red] {exc}")
        return 3

    counts = pipeline.registry.run_counts(run_id)
    console.print(
        f"Done={counts.get('done',0):,} · skipped={counts.get('skipped',0):,} · "
        f"partial={counts.get('partial',0):,} · failed={counts.get('failed',0):,}"
    )
    console.print(
        f"Physical bytes added: {_human_bytes(summary.physical_bytes_added)} · "
        f"logical bytes received: {_human_bytes(summary.logical_bytes)}"
    )
    return 0 if not counts.get("failed", 0) else 2


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
    return 0 if summary.errors == 0 else 2


def _cmd_status(args: argparse.Namespace) -> int:
    _, data_dir = _settings(args)
    registry = Registry(data_dir / "registry" / "corpus.db")
    registry.initialize()
    stats = registry.stats()
    pdf = registry.pdf_stats()
    refs = registry.reference_coverage()
    run = registry.latest_run()

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
        table.add_row("Latest run", f"#{run['id']} {run['mode']} · {run['status']}")
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
    rows = registry.iter_objects()
    bad = 0
    missing = 0
    with Progress(
        TextColumn("Verifying"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(), console=console
    ) as progress:
        task = progress.add_task("verify", total=len(rows))
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
    console.print(f"Verified {len(rows):,} object(s): bad={bad:,}, missing={missing:,}")
    return 0 if bad == 0 and missing == 0 else 2


def _cmd_bpk_discover(args: argparse.Namespace) -> int:
    cfg, _ = _settings(args)
    with _client(cfg) as client:
        connector = JdihBpkConnector(client)
        documents = list(connector.discover(query=args.query, year=args.year, page=args.page))
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
        documents = list(connector.discover(query=args.query, year=args.year, page=args.page))[: args.limit]
        for d in documents:
            d.metadata["year"] = args.year
        summary = pipeline.ingest_bpk(connector, documents, limit=args.limit, force=args.force)
    console.print_json(data={field: getattr(summary, field) for field in summary.__dataclass_fields__})
    return 0 if summary.errors == 0 else 2


def _add_scope_args(parser: argparse.ArgumentParser, *, allow_all: bool = True) -> None:
    parser.add_argument("--from-year", type=int, default=None)
    parser.add_argument("--to-year", type=int, default=None)
    if allow_all:
        parser.add_argument("--all", action="store_true", help="1945 through the current year.")
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

    setup = sub.add_parser("setup", help="Create config and initialize the local corpus.")
    setup.add_argument("--force", action="store_true")
    setup.set_defaults(func=_cmd_setup)

    plan = sub.add_parser("plan", help="Preview scope, storage and existing corpus stats.")
    _add_scope_args(plan)
    plan.set_defaults(func=_cmd_plan)

    backfill = sub.add_parser("backfill", help="Freeze and ingest a finite historical snapshot.")
    _add_scope_args(backfill)
    backfill.set_defaults(func=_cmd_backfill)

    sync = sub.add_parser("sync", help="Freeze a recent overlap window and detect new/changed records.")
    _add_scope_args(sync, allow_all=False)
    sync.set_defaults(func=_cmd_sync)

    retry = sub.add_parser("retry", help="Retry failed/partial items from a run.")
    retry.add_argument("--run-id", type=int, default=None)
    retry.add_argument("--no-progress", action="store_true")
    retry.set_defaults(func=_cmd_retry)

    status = sub.add_parser("status", help="Show corpus, storage, reference and run metrics.")
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
    discover.set_defaults(func=_cmd_bpk_discover)

    ingest = bpk_sub.add_parser("ingest")
    ingest.add_argument("--query", default=None)
    ingest.add_argument("--year", type=int, required=True)
    ingest.add_argument("--page", type=int, default=1)
    ingest.add_argument("--limit", type=int, default=10)
    ingest.add_argument("--force", action="store_true")
    ingest.set_defaults(func=_cmd_bpk_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
