from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Callable

from .integrity import validate_payload
from .models import SourceObservation, utc_now_iso
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

    def _check_storage(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.data_dir).free
        if self.min_free_bytes and free < self.min_free_bytes:
            raise StorageSafetyError(
                f"storage reserve reached: {free} bytes free, "
                f"minimum reserve is {self.min_free_bytes} bytes"
            )

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

                if not docs or signature == previous_signature:
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
            self.registry.set_run_status(run_id, "ready")
        else:
            self.registry.set_run_status(run_id, "partial")
        return total_added

    def _store_response(
        self,
        *,
        provider: str,
        source_url: str,
        source_id: str | None,
        response,
        kind: str,
        instrument_id: str | None,
        metadata: dict | None = None,
    ):
        retrieved_at = utc_now_iso()
        stored = self.archive.store(
            provider=provider,
            source_url=source_url,
            data=response.content,
            retrieved_at=retrieved_at,
            content_type=response.headers.get("content-type"),
            extra_metadata={"kind": kind, **(metadata or {})},
        )
        self.registry.record_object(stored, first_seen_at=retrieved_at)
        self.registry.record_source(
            SourceObservation(
                provider=provider,
                source_url=source_url,
                source_id=source_id,
                retrieved_at=retrieved_at,
                content_sha256=stored.sha256,
                mime_type=stored.mime_type,
                raw_path=str(stored.path),
                metadata={
                    "kind": kind,
                    "compression": stored.compression,
                    "logical_size": stored.logical_size,
                    "stored_size": stored.stored_size,
                    **(metadata or {}),
                },
            ),
            instrument_id=instrument_id,
        )
        return stored, retrieved_at

    def process_run(
        self,
        connector: JdihBpkConnector,
        run_id: int,
        *,
        refresh_known: bool = False,
        retry_failed: bool = False,
        callback: ProgressCallback | None = None,
    ) -> AcquisitionSummary:
        summary = AcquisitionSummary()
        run = self.registry.get_run(run_id)
        if not run:
            raise ValueError(f"unknown acquisition run {run_id}")
        self.registry.set_run_status(run_id, "running")

        if retry_failed:
            with self.registry.connect() as conn:
                items = list(
                    conn.execute(
                        """
                        SELECT * FROM manifest_items
                        WHERE run_id=? AND status IN ('pending','failed','partial')
                        ORDER BY year, id
                        """,
                        (run_id,),
                    )
                )
        else:
            items = self.registry.manifest_items(run_id)

        total_manifest = self.registry.manifest_count(run_id)
        initial_counts = self.registry.run_counts(run_id)
        already_terminal = sum(
            initial_counts.get(k, 0) for k in ("done", "skipped", "partial")
        )
        summary.discovered = total_manifest

        for offset, item in enumerate(items, start=1):
            self._check_storage()
            item_id = int(item["id"])
            document = DiscoveredDocument(
                provider=str(item["provider"]),
                source_id=str(item["source_id"]),
                detail_url=str(item["detail_url"]),
                title_hint=item["title_hint"],
                metadata={"year": int(item["year"])},
            )
            prior = self.registry.source_record(document.provider, document.source_id)

            if (
                prior
                and prior["latest_detail_sha256"]
                and not refresh_known
                and str(run["mode"]) == "backfill"
            ):
                self.registry.mark_manifest_item(item_id, "skipped", instrument_id=prior["instrument_id"])
                summary.skipped_existing += 1
                summary.processed += 1
                if callback:
                    callback(
                        {
                            "phase": "processing",
                            "run_id": run_id,
                            "completed": already_terminal + offset,
                            "total": total_manifest,
                            "source_id": document.source_id,
                            "status": "skipped",
                        }
                    )
                continue

            self.registry.mark_manifest_item(item_id, "running", increment_attempt=True)
            try:
                response, detail = connector.fetch_detail(document)
                out_of_scope = bool(
                    detail.instrument and detail.instrument.year != int(item["year"])
                )
                instrument_id = None
                if detail.instrument and not out_of_scope:
                    self.registry.upsert_instrument(detail.instrument)
                    instrument_id = detail.instrument.id
                    summary.ingested_instruments += 1
                elif detail.instrument is None:
                    summary.unresolved_details += 1

                detail_validation = validate_payload(
                    response.content,
                    content_type=response.headers.get("content-type"),
                    expected_mime="text/html",
                )
                stored_detail, checked_at = self._store_response(
                    provider=document.provider,
                    source_url=document.detail_url,
                    source_id=document.source_id,
                    response=response,
                    kind="detail_html",
                    instrument_id=instrument_id,
                    metadata={
                        "title_hint": document.title_hint,
                        "valid": detail_validation.valid,
                        "validation_reason": detail_validation.reason,
                    },
                )
                summary.archived_objects += 1
                summary.logical_bytes += stored_detail.logical_size
                if stored_detail.was_new:
                    summary.new_physical_objects += 1
                    summary.physical_bytes_added += stored_detail.stored_size

                previous_sha = prior["latest_detail_sha256"] if prior else None
                if out_of_scope:
                    self.registry.upsert_source_record(
                        document,
                        checked_at=checked_at,
                        detail_sha256=stored_detail.sha256,
                    )
                    self.registry.mark_manifest_item(item_id, "skipped")
                    summary.skipped_existing += 1
                    summary.processed += 1
                    if callback:
                        callback(
                            {
                                "phase": "processing",
                                "run_id": run_id,
                                "completed": already_terminal + offset,
                                "total": total_manifest,
                                "source_id": document.source_id,
                                "status": "out_of_scope",
                            }
                        )
                    continue

                self.registry.upsert_source_record(
                    document,
                    instrument_id=instrument_id,
                    checked_at=checked_at,
                    detail_sha256=stored_detail.sha256,
                )

                for ref in detail.references:
                    self.registry.record_reference(
                        ref, instrument_id=instrument_id, observed_at=checked_at
                    )
                    summary.references_recorded += 1

                if refresh_known and previous_sha and previous_sha == stored_detail.sha256:
                    self.registry.mark_manifest_item(item_id, "skipped", instrument_id=instrument_id)
                    summary.unchanged += 1
                    summary.processed += 1
                    if callback:
                        callback(
                            {
                                "phase": "processing",
                                "run_id": run_id,
                                "completed": already_terminal + offset,
                                "total": total_manifest,
                                "source_id": document.source_id,
                                "status": "unchanged",
                            }
                        )
                    continue

                item_partial = detail.instrument is None
                for file_url in detail.file_urls:
                    file_response = connector.fetch_file(file_url)
                    validation = validate_payload(
                        file_response.content,
                        content_type=file_response.headers.get("content-type"),
                        expected_mime="application/pdf",
                    )
                    stored_file, _ = self._store_response(
                        provider=document.provider,
                        source_url=file_url,
                        source_id=document.source_id,
                        response=file_response,
                        kind="document_file",
                        instrument_id=instrument_id,
                        metadata={
                            "detail_url": document.detail_url,
                            "valid": validation.valid,
                            "expected_mime": validation.expected_mime,
                            "validation_reason": validation.reason,
                        },
                    )
                    summary.archived_objects += 1
                    summary.logical_bytes += stored_file.logical_size
                    if stored_file.was_new:
                        summary.new_physical_objects += 1
                        summary.physical_bytes_added += stored_file.stored_size
                    if not validation.valid:
                        summary.invalid_files += 1
                        item_partial = True

                status = "partial" if item_partial else "done"
                self.registry.mark_manifest_item(item_id, status, instrument_id=instrument_id)
                summary.processed += 1
            except StorageSafetyError:
                self.registry.mark_manifest_item(item_id, "pending")
                self.registry.set_run_status(run_id, "partial")
                raise
            except Exception as exc:
                summary.errors += 1
                summary.processed += 1
                self.registry.mark_manifest_item(item_id, "failed", error=f"{type(exc).__name__}: {exc}")

            if callback:
                callback(
                    {
                        "phase": "processing",
                        "run_id": run_id,
                        "completed": already_terminal + offset,
                        "total": total_manifest,
                        "source_id": document.source_id,
                        "status": self.registry.run_counts(run_id),
                        "summary": summary,
                    }
                )

        counts = self.registry.run_counts(run_id)
        if counts.get("failed", 0) or counts.get("partial", 0) or counts.get("pending", 0):
            self.registry.set_run_status(run_id, "partial")
        else:
            self.registry.set_run_status(run_id, "complete")
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
