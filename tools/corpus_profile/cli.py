from __future__ import annotations

import argparse
import json
from pathlib import Path

from .profiler import final_validation, prepare_manifest, profile_manifest
from .statistics import summarize


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only PDF corpus characterization")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-count", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-file-bytes", type=int, default=1024**3)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    manifest = prepare_manifest(args.inventory, args.output, args.expected_count)
    profile_failed = False
    if not args.validate_only:
        profile_manifest(manifest, args.output, workers=args.workers, limit=args.limit,
                         max_file_bytes=args.max_file_bytes)
        summary = summarize(args.output, manifest)
        profile_failed = bool(summary["failed"])
        print(json.dumps({"profiled": summary["profiled"], "successful": summary["successful"], "failed": len(summary["failed"])}, sort_keys=True), flush=True)
    validation = final_validation(manifest, args.output)
    print(json.dumps({"source_validation_failures": len(validation["source_failures"])}), flush=True)
    return 1 if validation["source_failures"] or profile_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
