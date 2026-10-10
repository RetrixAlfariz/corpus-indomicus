"""Bounded, exact compression benchmarks for an inventory of PDF files.

The benchmark is intentionally stream based: source PDFs are never modified and
compressed data is kept in temporary files which are removed after each unit.
Use ``python -m tools.corpus_profile.benchmark --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import lzma
import multiprocessing
import platform
import shutil
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence

CHUNK = 1024 * 1024
METHODS = ("gzip9", "zstd3", "zstd9", "zstd19", "lzma2")
BENCHMARK_VERSION = "phase2-v2"


@dataclass(frozen=True)
class InventoryFile:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Unit:
    unit_id: str
    mode: str
    files: tuple[InventoryFile, ...]
    input_bytes: int


def load_inventory(path: str | Path) -> list[InventoryFile]:
    """Load and validate the compact file records from an inventory JSON."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    records = raw.get("files", raw) if isinstance(raw, dict) else raw
    result = []
    for item in records:
        p = Path(item["path"])
        size = int(item.get("logical_size", item.get("size", p.stat().st_size)))
        result.append(InventoryFile(p, str(item["sha256"]).lower(), size))
    return result


def bounded_files(files: Sequence[InventoryFile], max_docs: int | None = None,
                  max_input_bytes: int | None = None) -> list[InventoryFile]:
    """Apply deterministic document and source-byte limits before benchmarking."""
    selected: list[InventoryFile] = []
    total = 0
    for item in files:
        if max_docs is not None and len(selected) >= max_docs:
            break
        if max_input_bytes is not None and total + item.size > max_input_bytes:
            break
        selected.append(item)
        total += item.size
    return selected


def make_units(files: Sequence[InventoryFile], mode: str,
               shard_bytes: int = 512 * 1024 * 1024) -> list[Unit]:
    if mode == "perdoc":
        return [Unit(f"doc:{f.sha256}", mode, (f,), f.size) for f in files]
    if mode != "shard":
        raise ValueError("mode must be perdoc or shard")
    if shard_bytes <= 0:
        raise ValueError("shard_bytes must be positive")
    units: list[Unit] = []
    batch: list[InventoryFile] = []
    total = 0
    for item in files:
        if batch and total + item.size > shard_bytes:
            units.append(Unit(_shard_id(batch), mode, tuple(batch), total))
            batch, total = [], 0
        batch.append(item)
        total += item.size
    if batch:
        units.append(Unit(_shard_id(batch), mode, tuple(batch), total))
    return units


def _shard_id(files: Sequence[InventoryFile]) -> str:
    membership = "|".join(f"{item.sha256}:{item.size}" for item in files).encode()
    return "shard:" + hashlib.sha256(membership).hexdigest()[:24]


def _compressor(method: str):
    if method == "gzip9":
        return zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    if method.startswith("zstd"):
        try:
            import zstandard as zstd
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("zstandard is required for zstd methods") from exc
        return zstd.ZstdCompressor(level=int(method[4:])).compressobj()
    if method == "lzma2":
        return lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=6)
    raise ValueError(f"unknown method: {method}")


def _compress_stream(method: str, sources: Iterable[BinaryIO], target: BinaryIO) -> int:
    compressor = _compressor(method)
    written = 0
    for source in sources:
        while chunk := source.read(CHUNK):
            data = compressor.compress(chunk)
            if data:
                target.write(data)
                written += len(data)
    data = compressor.flush()
    if data:
        target.write(data)
        written += len(data)
    return written


def _decompress_stream(method: str, source: BinaryIO) -> Iterator[bytes]:
    if method == "gzip9":
        with gzip.GzipFile(fileobj=source, mode="rb") as stream:
            while chunk := stream.read(CHUNK):
                yield chunk
        return
    if method.startswith("zstd"):
        import zstandard as zstd
        with zstd.ZstdDecompressor().stream_reader(source) as stream:
            while chunk := stream.read(CHUNK):
                yield chunk
        return
    if method == "lzma2":
        stream = lzma.LZMAFile(source, mode="rb")
        try:
            while chunk := stream.read(CHUNK):
                yield chunk
        finally:
            stream.close()
        return
    raise ValueError(f"unknown method: {method}")


def _rss_bytes() -> int | None:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        try:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(value if platform.system() != "Windows" else value * 1024)
        except Exception:
            return None


def _pid_rss_bytes(pid: int) -> int | None:
    """Sample a live child RSS without relying on parent high-water marks."""
    try:
        import psutil
        return int(psutil.Process(pid).memory_info().rss)
    except Exception:
        if platform.system() == "Windows":
            try:
                import ctypes
                from ctypes import wintypes
                class Counters(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                                ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                                ("quota_peak", ctypes.c_size_t), ("quota", ctypes.c_size_t),
                                ("pool_peak", ctypes.c_size_t), ("pool", ctypes.c_size_t),
                                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]
                ctypes.windll.kernel32.OpenProcess.restype = ctypes.c_void_p
                ctypes.windll.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return None
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
                ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb)
                ctypes.windll.kernel32.CloseHandle(handle)
                return int(counters.ws) if ok else None
            except Exception:
                return None
        try:
            return int(Path(f"/proc/{pid}/status").read_text().split("VmRSS:", 1)[1].split()[0]) * 1024
        except Exception:
            return None


def _manifest(files: Sequence[InventoryFile]) -> tuple[bytes, list[tuple[int, int, InventoryFile]]]:
    offset = 0
    entries = []
    serial = []
    for item in files:
        entries.append((offset, item.size, item))
        serial.append({"name": item.sha256 + ".pdf", "offset": offset, "size": item.size,
                       "sha256": item.sha256})
        offset += item.size
    return json.dumps({"version": 1, "files": serial}, sort_keys=True,
                      separators=(",", ":")).encode(), entries


def _verify_shard(method: str, compressed: BinaryIO,
                  entries: Sequence[tuple[int, int, InventoryFile]]) -> tuple[bool, int, list[str]]:
    index = 0
    remaining = entries[0][1] if entries else 0
    hasher = hashlib.sha256()
    consumed = 0
    reconstructed: list[str] = []
    for chunk in _decompress_stream(method, compressed):
        cursor = 0
        while cursor < len(chunk) and index < len(entries):
            take = min(remaining, len(chunk) - cursor)
            hasher.update(chunk[cursor:cursor + take])
            cursor += take
            consumed += take
            remaining -= take
            if remaining == 0:
                if hasher.hexdigest() != entries[index][2].sha256:
                    return False, consumed, reconstructed
                reconstructed.append(hasher.hexdigest())
                index += 1
                if index < len(entries):
                    remaining = entries[index][1]
                    hasher = hashlib.sha256()
        if cursor != len(chunk):
            return False, consumed, reconstructed
    return index == len(entries) and remaining == 0, consumed, reconstructed


def benchmark_unit(unit: Unit, method: str, temp_dir: str | Path | None = None) -> dict:
    """Benchmark one unit; exceptions are deliberately left to the caller."""
    for item in unit.files:
        if item.path.stat().st_size != item.size:
            raise ValueError(f"inventory size mismatch: {item.path}")
    manifest, entries = _manifest(unit.files) if unit.mode == "shard" else (b"", [])
    def sources() -> Iterator[BinaryIO]:
        for item in unit.files:
            with item.path.open("rb") as handle:
                yield handle
    started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".compressed") as packed:
            payload_bytes = _compress_stream(method, sources(), packed)
            packed.flush()
            compress_seconds = time.perf_counter() - started
            packed.seek(0)
            decode_started = time.perf_counter()
            if unit.mode == "perdoc":
                digest = hashlib.sha256()
                decoded = 0
                for chunk in _decompress_stream(method, packed):
                    digest.update(chunk)
                    decoded += len(chunk)
                exact = decoded == unit.input_bytes and digest.hexdigest() == unit.files[0].sha256
                reconstructed = [digest.hexdigest()]
            else:
                exact, decoded, reconstructed = _verify_shard(method, packed, entries)
            decode_seconds = time.perf_counter() - decode_started
            output_bytes = payload_bytes + len(manifest)
            return {
                "unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                "status": "ok" if exact else "mismatch", "input_bytes": unit.input_bytes,
                "payload_bytes": payload_bytes, "manifest_bytes": len(manifest),
                "output_bytes": output_bytes, "ratio": output_bytes / unit.input_bytes,
                "saving": 1 - output_bytes / unit.input_bytes,
                "compress_seconds": compress_seconds, "decompress_seconds": decode_seconds,
                "compress_mib_s": unit.input_bytes / 2**20 / compress_seconds if compress_seconds else None,
                "decompress_mib_s": decoded / 2**20 / decode_seconds if decode_seconds else None,
                "exact": exact, "reconstructed_sha256": reconstructed,
                "independent_access": unit.mode == "perdoc",
                "peak_rss_bytes": _rss_bytes(), "error": "",
            }
    finally:
        # The source generator closes each file immediately after it is consumed.
        pass


FIELDS = ["unit_id", "mode", "method", "status", "input_bytes", "payload_bytes",
          "manifest_bytes", "output_bytes", "ratio", "saving", "compress_seconds",
          "decompress_seconds", "compress_mib_s", "decompress_mib_s", "exact",
          "reconstructed_sha256", "independent_access", "peak_rss_bytes", "error"]


def _worker(pipe, unit: Unit, method: str, temp_dir: str | Path | None) -> None:
    try:
        pipe.send(benchmark_unit(unit, method, temp_dir))
    except Exception as exc:
        pipe.send({"unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                   "status": "error", "input_bytes": unit.input_bytes,
                   "error": f"{type(exc).__name__}: {exc}"})
    finally:
        pipe.close()


def _run_isolated(unit: Unit, method: str, temp_dir: str | Path | None,
                  timeout_seconds: float, max_rss_bytes: int | None) -> dict:
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    worker_temp = tempfile.mkdtemp(prefix="corpus-benchmark-", dir=temp_dir)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(False)
    process = context.Process(target=_worker, args=(sender, unit, method, worker_temp))
    peak = None
    received = None
    try:
        process.start()
        sender.close()
        started = time.monotonic()
        while process.is_alive():
            # Drain the result before waiting for process exit. A large shard
            # receipt can otherwise fill the OS pipe and deadlock the worker.
            if receiver.poll():
                received = receiver.recv()
                break
            sample = _pid_rss_bytes(process.pid)
            if sample is not None:
                peak = max(peak or 0, sample)
                if max_rss_bytes is not None and sample > max_rss_bytes:
                    process.kill()
                    process.join()
                    return {"unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                            "status": "resource_limit", "input_bytes": unit.input_bytes,
                            "error": f"peak RSS exceeded {max_rss_bytes} bytes",
                            "peak_rss_bytes": peak}
            if time.monotonic() - started > timeout_seconds:
                process.kill()
                process.join()
                return {"unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                        "status": "timeout", "input_bytes": unit.input_bytes,
                        "error": f"timeout after {timeout_seconds:g}s", "peak_rss_bytes": peak}
            time.sleep(0.02)
        process.join()
        if received is None and receiver.poll():
            received = receiver.recv()
        if received is not None:
            if peak is not None:
                received["peak_rss_bytes"] = peak
            return received
        return {"unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                "status": "error", "input_bytes": unit.input_bytes,
                "error": f"worker exited with code {process.exitcode}", "peak_rss_bytes": peak}
    finally:
        try:
            sender.close()
        except Exception:
            pass
        try:
            receiver.close()
        except Exception:
            pass
        if process.is_alive():
            process.kill()
            process.join()
        target = Path(worker_temp).resolve()
        allowed_root = Path(temp_dir or tempfile.gettempdir()).resolve()
        if target.parent != allowed_root or not target.name.startswith("corpus-benchmark-"):
            raise RuntimeError("refusing cleanup outside the benchmark temporary root")
        shutil.rmtree(target, ignore_errors=True)


def run_benchmark(inventory: str | Path, output: str | Path, methods: Sequence[str] = METHODS,
                  modes: Sequence[str] = ("perdoc", "shard"), max_docs: int | None = None,
                  max_input_bytes: int | None = None, shard_bytes: int = 512 * 1024 * 1024,
                  temp_dir: str | Path | None = None, timeout_seconds: float = 300,
                  max_rss_bytes: int | None = 1024 * 1024 * 1024,
                  progress: bool = True) -> list[dict]:
    files = bounded_files(load_inventory(inventory), max_docs, max_input_bytes)
    units = [u for mode in modes for u in make_units(files, mode, shard_bytes)]
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    jsonl = out / "benchmark.jsonl"
    codec_versions = {"python": platform.python_version(), "gzip": "zlib-gzip",
                      "lzma2_preset": 6}
    try:
        import zstandard
        codec_versions["zstandard"] = zstandard.__version__
    except ImportError:
        codec_versions["zstandard"] = None
    identity = {"version": BENCHMARK_VERSION, "inventory": str(Path(inventory).resolve()),
                "files": [{"sha256": f.sha256, "size": f.size} for f in files],
                "methods": list(methods), "modes": list(modes), "shard_bytes": shard_bytes,
                "max_docs": max_docs, "max_input_bytes": max_input_bytes,
                "timeout_seconds": timeout_seconds, "max_rss_bytes": max_rss_bytes,
                "codec_versions": codec_versions}
    identity_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    meta = out / "benchmark_meta.json"
    if meta.exists():
        saved = json.loads(meta.read_text(encoding="utf-8"))
        if saved.get("identity_key") != identity_key:
            raise ValueError("output directory belongs to a different benchmark identity")
    else:
        meta.write_text(json.dumps({"identity_key": identity_key, "identity": identity}, indent=2), encoding="utf-8")
    completed = {}
    if jsonl.exists():
        raw_lines = jsonl.read_bytes().splitlines(keepends=True)
        lines = [line.decode("utf-8") for line in raw_lines]
        for line_number, line in enumerate(lines):
            if line.strip():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if line_number == len(lines) - 1:
                        # Remove the fragment before appending, otherwise the next
                        # result would turn it into an invalid interior line.
                        jsonl.write_bytes(b"".join(raw_lines[:-1]))
                        break
                    raise ValueError(f"invalid checkpoint JSON at line {line_number + 1}")
                completed[(row["unit_id"], row["method"])] = row
    results = list(completed.values())
    with jsonl.open("a", encoding="utf-8") as stream:
        for unit in units:
            for method in methods:
                key = (unit.unit_id, method)
                if key in completed:
                    continue
                if progress:
                    print(f"benchmark {unit.unit_id} {method}", flush=True)
                try:
                    row = _run_isolated(unit, method, temp_dir, timeout_seconds, max_rss_bytes)
                except Exception as exc:  # isolate unavailable compressors and bad files
                    status = "not_available" if isinstance(exc, ImportError) else "error"
                    row = {"unit_id": unit.unit_id, "mode": unit.mode, "method": method,
                           "status": status, "input_bytes": unit.input_bytes,
                           "error": f"{type(exc).__name__}: {exc}"}
                if row.get("status") == "error" and "zstandard is required" in row.get("error", ""):
                    row["status"] = "not_available"
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                results.append(row)
    with (out / "benchmark.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    (out / "benchmark_config.json").write_text(json.dumps({"inventory": str(inventory),
        "methods": list(methods), "modes": list(modes), "max_docs": max_docs,
        "max_input_bytes": max_input_bytes, "shard_bytes": shard_bytes}, indent=2), encoding="utf-8")
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--modes", default="perdoc,shard")
    parser.add_argument("--max-docs", type=int)
    parser.add_argument("--max-input-bytes", type=int)
    parser.add_argument("--shard-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--temp-dir")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--max-rss-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    run_benchmark(args.inventory, args.output, tuple(x for x in args.methods.split(",") if x),
                  tuple(x for x in args.modes.split(",") if x), args.max_docs,
                  args.max_input_bytes, args.shard_bytes, args.temp_dir,
                  args.timeout_seconds, args.max_rss_bytes, not args.quiet)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
