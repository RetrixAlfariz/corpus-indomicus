from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mime(data: bytes, content_type: str | None = None) -> str:
    """Use byte signatures first; HTTP Content-Type is only a fallback."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    prefix = data[:1024].lstrip().lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        return "text/html"
    if prefix.startswith((b"{", b"[")):
        return "application/json"

    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime and mime != "application/octet-stream":
            return mime
    return "application/octet-stream"


def extension_for_mime(mime: str) -> str:
    return {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "application/json": ".json",
        "text/plain": ".txt",
    }.get(mime.lower(), ".bin")


@dataclass(slots=True, frozen=True)
class ValidationResult:
    valid: bool
    detected_mime: str
    expected_mime: str | None = None
    reason: str | None = None


def validate_payload(
    data: bytes,
    *,
    content_type: str | None = None,
    expected_mime: str | None = None,
) -> ValidationResult:
    detected = detect_mime(data, content_type)
    if not data:
        return ValidationResult(False, detected, expected_mime, "empty_payload")
    if expected_mime and detected != expected_mime:
        return ValidationResult(
            False,
            detected,
            expected_mime,
            f"mime_mismatch:{detected}",
        )
    return ValidationResult(True, detected, expected_mime)
