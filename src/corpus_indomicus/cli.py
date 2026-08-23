from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .acquisition import AcquisitionPipeline
from .registry import Registry
from .sources.base import HttpSourceClient
from .sources.jdih_bpk import JdihBpkConnector


def _registry(data_dir: Path) -> Registry:
    return Registry(data_dir / "registry" / "corpus.db")


def _cmd_init(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    registry = _registry(data_dir)
    registry.initialize()
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    print(f"Initialized Corpus Indomicus data directory: {data_dir.resolve()}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    registry = _registry(Path(args.data_dir))
    registry.initialize()
    print(json.dumps(registry.stats(), indent=2))
    return 0


def _discover_bpk(args: argparse.Namespace) -> int:
    with HttpSourceClient(delay=args.delay) as client:
        connector = JdihBpkConnector(client)
        documents = list(
            connector.discover(query=args.query, year=args.year, page=args.page)
        )

    for document in documents[: args.limit]:
        print(
            json.dumps(
                {
                    "source_id": document.source_id,
                    "detail_url": document.detail_url,
                    "title_hint": document.title_hint,
                },
                ensure_ascii=False,
            )
        )
    print(f"Discovered {min(len(documents), args.limit)} document(s).", file=sys.stderr)
    return 0


def _ingest_bpk(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    pipeline = AcquisitionPipeline(data_dir=data_dir)

    with HttpSourceClient(delay=args.delay) as client:
        connector = JdihBpkConnector(client)
        documents = connector.discover(
            query=args.query,
            year=args.year,
            page=args.page,
        )
        summary = pipeline.ingest_bpk(
            connector,
            documents,
            limit=args.limit,
            force=args.force,
        )

    print(json.dumps({
        "discovered": summary.discovered,
        "ingested_instruments": summary.ingested_instruments,
        "archived_objects": summary.archived_objects,
        "skipped_seen_details": summary.skipped_seen_details,
        "unresolved_details": summary.unresolved_details,
        "errors": summary.errors,
    }, indent=2))
    return 0 if summary.errors == 0 else 2


def _add_common_bpk_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", default=None, help="Text query passed to JDIH BPK.")
    parser.add_argument("--year", type=int, default=None, help="Filter by year.")
    parser.add_argument("--page", type=int, default=0, help="Search result page.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum records this run.")
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Minimum seconds between HTTP requests.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus-indomicus",
        description="Acquisition toolkit for the Corpus Indomicus legal archive.",
    )
    parser.add_argument("--data-dir", default="data", help="Corpus data directory.")

    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize local archive and registry.")
    init.set_defaults(func=_cmd_init)

    status = sub.add_parser("status", help="Show local registry counts.")
    status.set_defaults(func=_cmd_status)

    bpk = sub.add_parser("bpk", help="JDIH BPK source operations.")
    bpk_sub = bpk.add_subparsers(dest="bpk_command", required=True)

    discover = bpk_sub.add_parser("discover", help="List discoverable BPK records.")
    _add_common_bpk_args(discover)
    discover.set_defaults(func=_discover_bpk)

    ingest = bpk_sub.add_parser("ingest", help="Archive BPK details and document files.")
    _add_common_bpk_args(ingest)
    ingest.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch detail pages already recorded in the registry.",
    )
    ingest.set_defaults(func=_ingest_bpk)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
