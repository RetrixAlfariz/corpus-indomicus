from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import LegalInstrument
from .base import DiscoveredDocument, HttpSourceClient, SourceConnector


PROVIDER = "jdih_bpk"
BASE_URL = "https://peraturan.bpk.go.id"


@dataclass(slots=True)
class BpkDetail:
    instrument: LegalInstrument | None
    metadata: dict[str, str]
    file_urls: list[str]


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


def _details_id(url: str) -> str:
    match = re.search(r"/Details/(\d+)", url, flags=re.IGNORECASE)
    return match.group(1) if match else url


def _with_params(url: str, params: dict[str, Any]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items() if v is not None and v != ""})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def parse_search_html(html: str, base_url: str = BASE_URL) -> list[DiscoveredDocument]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, DiscoveredDocument] = {}

    for anchor in soup.select('a[href*="/Details/"]'):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        detail_url = urljoin(base_url, href)
        source_id = _details_id(detail_url)
        if source_id in found:
            continue

        title_hint = _clean(anchor.get_text(" ", strip=True)) or None
        if not title_hint:
            parent = anchor.find_parent(["article", "div", "li"])
            if parent:
                title_hint = _clean(parent.get_text(" ", strip=True))[:500] or None

        found[source_id] = DiscoveredDocument(
            provider=PROVIDER,
            source_id=source_id,
            detail_url=detail_url,
            title_hint=title_hint,
        )

    return list(found.values())


def _metadata_from_tables(soup: BeautifulSoup) -> dict[str, str]:
    metadata: dict[str, str] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            key = _clean(cells[0].get_text(" ", strip=True))
            value = _clean(cells[1].get_text(" ", strip=True))
            if key and value:
                metadata.setdefault(key, value)

    for term in soup.find_all("dt"):
        desc = term.find_next_sibling("dd")
        if desc:
            key = _clean(term.get_text(" ", strip=True))
            value = _clean(desc.get_text(" ", strip=True))
            if key and value:
                metadata.setdefault(key, value)

    return metadata


KNOWN_LABELS = (
    "Tipe Dokumen",
    "Judul",
    "T.E.U.",
    "Nomor",
    "Bentuk",
    "Bentuk Singkat",
    "Tahun",
    "Tempat Penetapan",
    "Tanggal Penetapan",
    "Tanggal Pengundangan",
    "Tanggal Berlaku",
    "Sumber",
    "Subjek",
    "Status",
    "Bahasa",
    "Lokasi",
    "Bidang",
)


def _metadata_from_text(soup: BeautifulSoup) -> dict[str, str]:
    """Fallback for pages whose metadata is laid out as adjacent block elements."""
    result: dict[str, str] = {}
    strings = [_clean(s) for s in soup.stripped_strings]
    positions = {label: i for i, text in enumerate(strings) for label in KNOWN_LABELS if text == label}

    for label, index in positions.items():
        for candidate in strings[index + 1 : index + 5]:
            if candidate in KNOWN_LABELS:
                break
            if candidate:
                result.setdefault(label, candidate)
                break
    return result


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = _clean(value)
    months = {
        "januari": "January",
        "februari": "February",
        "maret": "March",
        "april": "April",
        "mei": "May",
        "juni": "June",
        "juli": "July",
        "agustus": "August",
        "september": "September",
        "oktober": "October",
        "november": "November",
        "desember": "December",
    }
    translated = value.lower()
    for indo, english in months.items():
        translated = re.sub(rf"\b{indo}\b", english, translated, flags=re.IGNORECASE)
    try:
        return datetime.strptime(translated.title(), "%d %B %Y").date().isoformat()
    except ValueError:
        return value


def _find_files(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue

        text = _clean(anchor.get_text(" ", strip=True)).lower()
        href_lower = href.lower()
        looks_like_file = (
            href_lower.endswith(".pdf")
            or ".pdf?" in href_lower
            or "/download" in href_lower
            or "download" in text
        )
        if not looks_like_file:
            continue

        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)

    return urls


def parse_detail_html(html: str, detail_url: str) -> BpkDetail:
    soup = BeautifulSoup(html, "html.parser")

    metadata = _metadata_from_tables(soup)
    for key, value in _metadata_from_text(soup).items():
        metadata.setdefault(key, value)

    document_type = metadata.get("Bentuk Singkat") or metadata.get("Bentuk")
    number = metadata.get("Nomor")
    year = _parse_year(metadata.get("Tahun"))
    title = metadata.get("Judul")

    if not title:
        heading = soup.find("h1")
        title = _clean(heading.get_text(" ", strip=True)) if heading else None

    instrument: LegalInstrument | None = None
    if document_type and number and year and title:
        instrument = LegalInstrument.from_minimal(
            document_type=document_type,
            number=number,
            year=year,
            title=title,
            issuing_body=metadata.get("T.E.U."),
            status=metadata.get("Status"),
            dates={
                "enacted": _parse_date(metadata.get("Tanggal Penetapan")),
                "promulgated": _parse_date(metadata.get("Tanggal Pengundangan")),
                "effective": _parse_date(metadata.get("Tanggal Berlaku")),
            },
            publication={"source": metadata.get("Sumber")},
            metadata={
                "source_metadata": metadata,
                "bpk_detail_id": _details_id(detail_url),
            },
        )

    return BpkDetail(
        instrument=instrument,
        metadata=metadata,
        file_urls=_find_files(soup, detail_url),
    )


class JdihBpkConnector(SourceConnector):
    provider = PROVIDER

    def __init__(self, client: HttpSourceClient):
        self.client = client

    def discover(
        self,
        *,
        query: str | None = None,
        year: int | None = None,
        page: int = 0,
    ) -> Iterable[DiscoveredDocument]:
        url = _with_params(
            f"{BASE_URL}/Search",
            {
                "tentang": query,
                "tahun": year,
                "page": page,
            },
        )
        response = self.client.get(url)
        return parse_search_html(response.text, BASE_URL)

    def fetch_detail(self, document: DiscoveredDocument):
        response = self.client.get(document.detail_url)
        return response, parse_detail_html(response.text, document.detail_url)

    def fetch_file(self, url: str):
        return self.client.get(url)
