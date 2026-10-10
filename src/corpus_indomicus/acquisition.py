from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import json
import os
from contextlib import contextmanager
from .integrity import sha256_bytes, detect_mime
from .storage import StoredObject
from typing import Callable

from .integrity import validate_payload
from .models import SourceObservation, utc_now_iso, make_instrument_id
from .registry import Registry
from .storage import RawArchive
from .sources.base import DiscoveredDocument
from .sources.jdih_bpk import JdihBpkConnector


ProgressCallback = Callable[[dict], None]


class StorageSafetyError(RuntimeError):
    pass


@dataclass(slots=True)
class AcquisitionSummary:
    discovered: int = 0
    processed: int = 0
    ingested_instruments: int = 0
    archived_objects: int = 0
    new_physical_objects: int = 0
    references_recorded: int = 0
    skipped_existing: int = 0
    unchanged: int = 0
    invalid_files: int = 0
    unresolved_details: int = 0
    errors: int = 0
    logical_bytes: int = 0
    physical_bytes_added: int = 0


class AcquisitionPipeline:
    def __init__(self, *, data_dir: str | Path, min_free_bytes: int = 0):
        self.data_dir = Path(data_dir)
        self.registry = Registry(self.data_dir / "registry" / "corpus.db")
        self.archive = RawArchive(self.data_dir / "objects")
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.registry.initialize()
        with self._run_lock(0):
            self._recover_receipts()

    def _check_storage(self, additional_bytes=0) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.data_dir).free
        if self.min_free_bytes and free - additional_bytes < self.min_free_bytes:
            raise StorageSafetyError(
                f"storage reserve reached: {free} bytes free, "
                f"minimum reserve is {self.min_free_bytes} bytes"
            )

    @contextmanager
    def _run_lock(self, run_id):
        """One writer per archive; OS releases the lock on process death."""
        folder = self.data_dir / "locks"
        folder.mkdir(exist_ok=True)
        with (folder / "acquisition.lock").open("a+b") as handle:
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"run {run_id} is already active") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def create_or_resume_run(
        self,
        *,
        mode: str,
        provider: str,
        from_year: int,
        to_year: int,
        snapshot_cutoff: str,
        resume: bool = True,
    ) -> tuple[int, bool]:
        if resume:
            row = self.registry.find_resumable_run(
                mode=mode,
                provider=provider,
                from_year=from_year,
                to_year=to_year,
            )
            if row:
                return int(row["id"]), True
        return (
            self.registry.create_run(
                mode=mode,
                provider=provider,
                from_year=from_year,
                to_year=to_year,
                snapshot_cutoff=snapshot_cutoff,
            ),
            False,
        )

    def discover_snapshot(
        self,
        connector: JdihBpkConnector,
        run_id: int,
        *,
        query: str | None = None,
        callback: ProgressCallback | None = None,
        max_pages_per_year: int = 10000,
    ) -> int:
        """Build a finite manifest before document downloads begin."""
        run = self.registry.get_run(run_id)
        if run and run["scope_json"] and run["scope_json"] != "{}":
            return self.discover_scoped(connector, run_id, query=query,
                                        callback=callback, max_pages=max_pages_per_year)
        if run and run["frozen_at"]:
            return 0
        total_added = 0
        segments = self.registry.segments(run_id)
        for segment in segments:
            year = int(segment["year"])
            if segment["status"] == "complete":
                continue

            page = max(1, int(segment["next_page"]))
            discovered_count = int(segment["discovered_count"])
            previous_signature: tuple[str, ...] | None = None
            self.registry.update_segment(run_id, year, status="discovering")

            pages_seen = 0
            while pages_seen < max_pages_per_year:
                search_page = connector.search_page(query=query, year=year, page=page)
                docs = search_page.documents
                signature = tuple(doc.source_id for doc in docs)

                if signature == previous_signature and docs:
                    self.registry.update_segment(run_id, year, status="partial")
                    raise RuntimeError("repeated search page; discovery remains incomplete")
                if not docs:
                    if search_page.reported_total is None or discovered_count < search_page.reported_total:
                        self.registry.update_segment(run_id, year, status="partial")
                        raise RuntimeError("unverified empty search page")
                    self.registry.update_segment(
                        run_id,
                        year,
                        next_page=page,
                        status="complete",
                        reported_total=search_page.reported_total,
                        discovered_count=discovered_count,
                    )
                    break

                added = self.registry.add_manifest_items(run_id, year, docs)
                total_added += added
                discovered_count += added
                previous_signature = signature
                pages_seen += 1

                self.registry.update_segment(
                    run_id,
                    year,
                    next_page=page + 1,
                    status="discovering",
                    reported_total=search_page.reported_total,
                    discovered_count=discovered_count,
                )
                if callback:
                    callback(
                        {
                            "phase": "discovery",
                            "run_id": run_id,
                            "year": year,
                            "page": page,
                            "discovered": discovered_count,
                            "reported_total": search_page.reported_total,
                        }
                    )

                # Reported totals are informational only. Search cards may include
                # same-year reference links, so using that count as a hard stop could
                # terminate pagination before all primary results have been visited.
                page += 1
            else:
                self.registry.update_segment(run_id, year, status="partial")

        if all(row["status"] == "complete" for row in self.registry.segments(run_id)):
            self.registry.freeze_manifest(run_id)
            self.registry.set_run_status(run_id, "ready")
        else:
            self.registry.set_run_status(run_id, "partial")
        return total_added

    def discover_scoped(self, connector, run_id, *, query=None, callback=None, max_pages=10000):
        with self._run_lock(run_id):
            return self._discover_scoped(connector, run_id, query=query, callback=callback, max_pages=max_pages)

    def _discover_scoped(self, connector, run_id, *, query=None, callback=None, max_pages=10000):
        run = self.registry.get_run(run_id)
        if run["frozen_at"]:
            return 0
        scope = json.loads(run["scope_json"])
        if query is not None and query != scope.get("query"):
            raise ValueError("query differs from frozen scope; create a new run")
        total_added = 0
        for partition in self.registry.partitions(run_id):
            if partition["status"] == "complete":
                continue
            group, type_id, year = partition["group"], partition["type_id"], partition["year"]
            page = partition["next_page"]
            count = partition["discovered_count"]
            previous = partition["last_signature"]
            try:
                for _ in range(max_pages):
                    self._check_storage()
                    result = connector.search_page(query=scope.get("query"), year=year or None,
                                                   type_id=type_id, page=page)
                    signature = sha256_bytes(json.dumps([d.source_id for d in result.documents]).encode())
                    if not result.parser_ok or (result.documents and signature == previous):
                        raise RuntimeError("unverified or repeated search page")
                    if result.has_more is None:
                        raise RuntimeError("pagination termination cannot be verified")
                    if not result.documents and (result.has_more or result.reported_total != 0):
                        raise RuntimeError("unexpected empty results")
                    for doc in result.documents:
                        doc.metadata.update({"group": group, "type_id": type_id})
                    total_added += self.registry.add_manifest_items(run_id, year, result.documents,
                                                                    group=group, type_id=type_id)
                    # Membership count is authoritative after replay or cross-group dedup.
                    with self.registry.connect() as conn:
                        count = conn.execute('SELECT COUNT(*) FROM discovery_memberships WHERE run_id=? AND "group"=? AND type_id=? AND year=?',
                                             (run_id, group, type_id, year)).fetchone()[0]
                    next_page = result.next_page or page + 1
                    self.registry.update_partition(run_id, group, type_id, year,
                        next_page=next_page, status="complete" if not result.has_more else "discovering",
                        reported_total=result.reported_total, discovered_count=count, last_signature=signature)
                    if callback:
                        callback({"phase": "discovery", "run_id": run_id, "year": year or "all",
                                  "group": group, "type_id": type_id, "page": page,
                                  "discovered": count, "reported_total": result.reported_total})
                    if not result.has_more:
                        break
                    previous, page = signature, next_page
                else:
                    self.registry.update_partition(run_id, group, type_id, year, status="partial",
                                                   last_error="page limit reached")
            except Exception as exc:
                self.registry.update_partition(run_id, group, type_id, year, status="partial",
                                               last_error=f"{type(exc).__name__}: {exc}")
                self.registry.set_run_status(run_id, "partial")
                raise
        if all(p["status"] == "complete" for p in self.registry.partitions(run_id)):
            self.registry.freeze_manifest(run_id)
            self.registry.set_run_status(run_id, "ready")
        else:
            self.registry.set_run_status(run_id, "partial")
        return total_added

    def _recover_receipts(self) -> None:
        """Replay durable storage intents after an object/SQLite crash window."""
        folder = self.data_dir / "receipts"
        if not folder.exists():
            return
        for receipt in folder.glob("*.json"):
            intent = json.loads(receipt.read_text(encoding="utf-8"))
            obj = intent["object"]
            path = Path(obj["path"])
            if not path.exists():
                continue  # Interrupted before rename; normal retry completes it.
            content = RawArchive.read_object(path, compression=obj["compression"])
            if sha256_bytes(content) != obj["sha256"]:
                raise ValueError(f"recovery object checksum mismatch: {path}")
            obj["path"] = path
            obj["stored_size"] = path.stat().st_size
            intent["observation"]["metadata"]["stored_size"] = obj["stored_size"]
            self.registry.record_object(StoredObject(**obj), first_seen_at=intent["observation"]["retrieved_at"])
            self.registry.record_source(SourceObservation(**intent["observation"]), intent["instrument_id"])
            receipt.unlink()

    def _store_response(self, *, provider, source_url, source_id, response,
                        kind, instrument_id, metadata=None):
        retrieved_at = utc_now_iso()
        mime = detect_mime(response.content, response.headers.get("content-type"))
        compression = "gzip" if mime in self.archive.COMPRESSIBLE else None
        digest = sha256_bytes(response.content)
        path = self.archive._path_for(digest, mime, compression)
        http_metadata = {
            "http_status": getattr(response, "status_code", 200),
            "response_url": str(getattr(response, "url", source_url)),
            "http_headers": {k: v for k, v in response.headers.items()
                             if k.lower() in {"content-type", "content-length", "etag", "last-modified", "date", "retry-after", "content-encoding"}},
        }
        observation = SourceObservation(provider=provider, source_url=source_url,
            source_id=source_id, retrieved_at=retrieved_at, content_sha256=digest,
            mime_type=mime, raw_path=str(path), metadata={"kind": kind,
                "compression": compression, "logical_size": len(response.content),
                **http_metadata, **(metadata or {})})
        folder = self.data_dir / "receipts"
        folder.mkdir(exist_ok=True)
        receipt = folder / f"{sha256_bytes((provider + source_url + digest).encode())}.json"
        intent = {"object": {"sha256": digest, "mime_type": mime, "path": str(path),
            "logical_size": len(response.content), "stored_size": 0,
            "compression": compression, "was_new": False},
            "observation": observation.to_dict(), "instrument_id": instrument_id}
        temporary = receipt.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(intent, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt)
        self._check_storage(len(response.content) if not path.exists() else 0)
        stored = self.archive.store(provider=provider, source_url=source_url,
            data=response.content, retrieved_at=retrieved_at,
            content_type=response.headers.get("content-type"))
        self.registry.record_object(stored, first_seen_at=retrieved_at)
        observation.metadata["stored_size"] = stored.stored_size
        self.registry.record_source(observation, instrument_id=instrument_id)
        receipt.unlink()
        return stored, retrieved_at

    def process_run(self, connector, run_id, *, refresh_known=False,
                    retry_failed=False, callback=None, limit=None):
        with self._run_lock(run_id):
            return self._process_run(connector, run_id, refresh_known=refresh_known,
                                     retry_failed=retry_failed, callback=callback, limit=limit)

    def _process_run(self, connector, run_id, *, refresh_known=False,
                     retry_failed=False, callback=None, limit=None):
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        run = self.registry.get_run(run_id)
        if not run:
            raise ValueError(f"unknown acquisition run {run_id}")
        if not run["frozen_at"]:
            self.registry.freeze_manifest(run_id)
        summary = AcquisitionSummary(discovered=self.registry.manifest_count(run_id))
        self.registry.set_run_status(run_id, "running")
        for item in self.registry.iter_manifest_items(run_id, retry_failed=retry_failed, limit=limit):
            self._check_storage()
            item_id = int(item["id"])
            metadata = json.loads(item["metadata_json"])
            metadata.setdefault("year", int(item["year"]))
            document = DiscoveredDocument(item["provider"], item["source_id"],
                                          item["detail_url"], item["title_hint"], metadata)
            self.registry.mark_manifest_item(item_id, "running", increment_attempt=True)
            status = "failed"
            try:
                response, detail = connector.fetch_detail(document)
                instrument_id = None
                if detail.instrument:
                    # A provider record remains distinct even when issuer normalization
                    # is missing or two institutions share type/year/number.
                    detail.instrument.metadata["legacy_identity_candidate"] = detail.instrument.id
                    if detail.instrument.issuing_body:
                        detail.instrument.metadata["issuer_identity_candidate"] = make_instrument_id(
                            detail.instrument.document_type, detail.instrument.year, detail.instrument.number,
                            detail.instrument.jurisdiction, issuing_body=detail.instrument.issuing_body)
                    detail.instrument.id = f"{document.provider}:source:{document.source_id}"
                    self.registry.upsert_instrument(detail.instrument)
                    instrument_id = detail.instrument.id
                    summary.ingested_instruments += 1
                else:
                    summary.unresolved_details += 1
                stored, checked_at = self._store_response(provider=document.provider,
                    source_url=document.detail_url, source_id=document.source_id,
                    response=response, kind="detail_html", instrument_id=instrument_id,
                    metadata={"source_metadata": detail.metadata, "discovery": metadata})
                summary.archived_objects += 1
                summary.logical_bytes += stored.logical_size
                if stored.was_new:
                    summary.new_physical_objects += 1
                    summary.physical_bytes_added += stored.stored_size
                self.registry.upsert_source_record(document, instrument_id=instrument_id,
                    checked_at=checked_at, detail_sha256=stored.sha256)
                self.registry.mark_manifest_item(item_id, "running", detail_sha256=stored.sha256)
                scope = json.loads(run["scope_json"] or "{}")
                year = detail.instrument.year if detail.instrument else None
                location = detail.metadata.get("Lokasi", "").lower()
                regional = any(w in location for w in ("kabupaten", "kota ", "provinsi")) or any(
                    w in detail.metadata.get("Bentuk", "").lower()
                    for w in ("peraturan daerah", "peraturan bupati", "peraturan walikota", "peraturan gubernur"))
                wrong_year = bool(year and item["year"] and year != item["year"])
                source_type = detail.metadata.get("Jenis ID")
                expected_types = {t["type_id"]: t.get("name", "") for t in scope.get("types", [])}
                classification_unresolved = False
                if expected_types:
                    with self.registry.connect() as conn:
                        member_types = {r[0] for r in conn.execute("SELECT type_id FROM discovery_memberships WHERE run_id=? AND provider=? AND source_id=?",
                            (run_id, document.provider, document.source_id))}
                    if source_type:
                        classification_unresolved = source_type not in member_types
                    else:
                        observed_type = " ".join(detail.metadata.get("Bentuk", "").casefold().split())
                        classification_unresolved = not any(observed_type == " ".join(expected_types.get(t, "").casefold().split())
                                                            for t in member_types if expected_types.get(t))
                if wrong_year or regional:
                    status = "out_of_scope"
                else:
                    for ref in detail.references:
                        self.registry.record_reference(ref, instrument_id=instrument_id, observed_at=checked_at)
                        summary.references_recorded += 1
                    self.registry.set_expected_files(document.provider, document.source_id,
                                                     stored.sha256, detail.file_urls)
                    known_files = {row["url"]: row for row in self.registry.expected_files(
                        document.provider, document.source_id, stored.sha256)}
                    failures = 0
                    for file_url in detail.file_urls:
                        self._check_storage()
                        try:
                            known = known_files.get(file_url)
                            if not refresh_known and known and known["status"] == "downloaded" and known["sha256"]:
                                with self.registry.connect() as conn:
                                    obj = conn.execute("SELECT * FROM objects WHERE sha256=?", (known["sha256"],)).fetchone()
                                if obj and Path(obj["path"]).exists():
                                    content = RawArchive.read_object(obj["path"], compression=obj["compression"])
                                    if sha256_bytes(content) == known["sha256"] and validate_payload(content, expected_mime="application/pdf").valid:
                                        summary.skipped_existing += 1
                                        continue
                            file_response = connector.fetch_file(file_url)
                            validation = validate_payload(file_response.content,
                                content_type=file_response.headers.get("content-type"),
                                expected_mime="application/pdf")
                            file_obj, _ = self._store_response(provider=document.provider,
                                source_url=file_url, source_id=document.source_id,
                                response=file_response, kind="document_file", instrument_id=instrument_id,
                                metadata={"detail_url": document.detail_url, "detail_sha256": stored.sha256,
                                    "valid": validation.valid, "validation_reason": validation.reason})
                            self.registry.record_file(document.provider, document.source_id, stored.sha256, file_url,
                                status="downloaded" if validation.valid else "failed", sha256=file_obj.sha256,
                                error=validation.reason)
                            summary.archived_objects += 1
                            summary.logical_bytes += file_obj.logical_size
                            if file_obj.was_new:
                                summary.new_physical_objects += 1
                                summary.physical_bytes_added += file_obj.stored_size
                            if not validation.valid:
                                summary.invalid_files += 1
                                failures += 1
                        except StorageSafetyError:
                            raise
                        except Exception as exc:
                            failures += 1
                            self.registry.record_file(document.provider, document.source_id, stored.sha256, file_url,
                                status="failed", error=f"{type(exc).__name__}: {exc}")
                    if not detail.instrument or classification_unresolved:
                        status = "unresolved"
                    elif not detail.file_urls:
                        status = "no_file"
                    elif failures or not self.registry.files_complete(document.provider, document.source_id, stored.sha256):
                        status = "partial"
                    else:
                        status = "done"
                self.registry.mark_manifest_item(item_id, status, instrument_id=instrument_id)
            except StorageSafetyError:
                self.registry.mark_manifest_item(item_id, "pending")
                self.registry.set_run_status(run_id, "partial")
                raise
            except Exception as exc:
                summary.errors += 1
                self.registry.mark_manifest_item(item_id, "failed", error=f"{type(exc).__name__}: {exc}")
            summary.processed += 1
            if callback:
                callback({"phase": "processing", "run_id": run_id, "completed": summary.processed,
                          "total": summary.discovered, "source_id": document.source_id, "status": status})
        counts = self.registry.run_counts(run_id)
        incomplete = any(counts.get(s, 0) for s in ("pending", "running", "failed", "partial", "unresolved"))
        self.registry.set_run_status(run_id, "partial" if incomplete else "complete")
        return summary

    def ingest_bpk(
        self,
        connector: JdihBpkConnector,
        documents,
        *,
        limit: int | None = None,
        force: bool = False,
    ) -> AcquisitionSummary:
        docs = list(documents)
        if limit is not None:
            docs = docs[:limit]
        years = [int(d.metadata.get("year")) for d in docs if d.metadata.get("year")]
        year = years[0] if years else 2025
        run_id = self.registry.create_run(
            mode="backfill",
            provider=connector.provider,
            from_year=year,
            to_year=year,
            snapshot_cutoff=utc_now_iso(),
        )
        self.registry.add_manifest_items(run_id, year, docs)
        self.registry.update_segment(run_id, year, status="complete", discovered_count=len(docs))
        self.registry.set_run_status(run_id, "ready")
        return self.process_run(connector, run_id, refresh_known=force)
