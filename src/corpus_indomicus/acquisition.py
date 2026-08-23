from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import SourceObservation, utc_now_iso
from .registry import Registry
from .storage import RawArchive
from .sources.base import DiscoveredDocument
from .sources.jdih_bpk import JdihBpkConnector


@dataclass(slots=True)
class AcquisitionSummary:
    discovered: int = 0
    ingested_instruments: int = 0
    archived_objects: int = 0
    skipped_seen_details: int = 0
    unresolved_details: int = 0
    errors: int = 0


class AcquisitionPipeline:
    def __init__(self, *, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.registry = Registry(self.data_dir / "registry" / "corpus.db")
        self.archive = RawArchive(self.data_dir / "raw")
        self.registry.initialize()

    def ingest_bpk(
        self,
        connector: JdihBpkConnector,
        documents: Iterable[DiscoveredDocument],
        *,
        limit: int | None = None,
        force: bool = False,
    ) -> AcquisitionSummary:
        summary = AcquisitionSummary()

        for index, document in enumerate(documents):
            if limit is not None and index >= limit:
                break

            summary.discovered += 1
            if not force and self.registry.source_seen(
                document.provider, document.detail_url
            ):
                summary.skipped_seen_details += 1
                continue

            try:
                response, detail = connector.fetch_detail(document)
                retrieved_at = utc_now_iso()
                stored_detail = self.archive.store(
                    provider=document.provider,
                    source_url=document.detail_url,
                    data=response.content,
                    retrieved_at=retrieved_at,
                    content_type=response.headers.get("content-type"),
                    extra_metadata={"kind": "detail_html", "source_id": document.source_id},
                )
                summary.archived_objects += 1

                instrument_id = detail.instrument.id if detail.instrument else None
                if detail.instrument:
                    self.registry.upsert_instrument(detail.instrument)
                    summary.ingested_instruments += 1
                else:
                    summary.unresolved_details += 1

                self.registry.record_source(
                    SourceObservation(
                        provider=document.provider,
                        source_url=document.detail_url,
                        source_id=document.source_id,
                        retrieved_at=retrieved_at,
                        content_sha256=stored_detail.sha256,
                        mime_type=stored_detail.mime_type,
                        raw_path=str(stored_detail.path),
                        metadata={
                            "kind": "detail_html",
                            "title_hint": document.title_hint,
                            "parsed_metadata": detail.metadata,
                        },
                    ),
                    instrument_id=instrument_id,
                )

                for file_url in detail.file_urls:
                    file_response = connector.fetch_file(file_url)
                    file_retrieved_at = utc_now_iso()
                    stored_file = self.archive.store(
                        provider=document.provider,
                        source_url=file_url,
                        data=file_response.content,
                        retrieved_at=file_retrieved_at,
                        content_type=file_response.headers.get("content-type"),
                        extra_metadata={
                            "kind": "document_file",
                            "detail_url": document.detail_url,
                            "source_id": document.source_id,
                        },
                    )
                    summary.archived_objects += 1
                    self.registry.record_source(
                        SourceObservation(
                            provider=document.provider,
                            source_url=file_url,
                            source_id=document.source_id,
                            retrieved_at=file_retrieved_at,
                            content_sha256=stored_file.sha256,
                            mime_type=stored_file.mime_type,
                            raw_path=str(stored_file.path),
                            metadata={
                                "kind": "document_file",
                                "detail_url": document.detail_url,
                            },
                        ),
                        instrument_id=instrument_id,
                    )
            except Exception:
                summary.errors += 1

        return summary
