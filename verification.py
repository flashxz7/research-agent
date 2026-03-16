# -*- coding: utf-8 -*-
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_FETCH_TIMEOUT = 12
_FETCH_RETRIES = 2
_RETRY_BACKOFF = 1.8
_MAX_TEXT_CHARS = 200_000
_MAX_SENTENCES = 2_000
_FUZZY_SEQ_THRESHOLD = 0.58
_TOKEN_OVERLAP_THRESHOLD = 0.55
_MIN_KEEP_SCORE = 0.25

_CITATION_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Status(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"
    UNREACHABLE = "UNREACHABLE"


@dataclass
class PageContent:
    text: str
    sentences: list[str]
    text_lower: str


@dataclass
class ClaimEvidence:
    citation_index: int
    url: str
    status: Status
    quote: str = ""
    score: float = 0.0
    note: str = ""


@dataclass
class ClaimCheck:
    claim: str
    raw_sentence: str
    citation_indices: list[int]
    citations: list[ClaimEvidence] = field(default_factory=list)
    status: Status = Status.UNVERIFIED
    note: str = ""
    best_score: float = 0.0


@dataclass
class VerificationReport:
    claims: list[ClaimCheck]

    @property
    def summary(self) -> str:
        counts: dict[Status, int] = {s: 0 for s in Status}
        for c in self.claims:
            counts[c.status] += 1
        return "  ".join(f"{v} {k.value}" for k, v in counts.items() if v)

    @property
    def verified_ratio(self) -> float:
        if not self.claims:
            return 0.0
        unreachable = sum(1 for c in self.claims if c.status == Status.UNREACHABLE)
        denom = max(len(self.claims) - unreachable, 0)
        if denom == 0:
            return 0.0
        good = sum(1 for c in self.claims if c.status in (Status.VERIFIED, Status.PARTIAL))
        return good / denom

    @property
    def verified_claims(self) -> list[ClaimCheck]:
        return [c for c in self.claims if c.status in (Status.VERIFIED, Status.PARTIAL)]

    @property
    def unverified_claims(self) -> list[ClaimCheck]:
        return [c for c in self.claims if c.status == Status.UNVERIFIED]

    @property
    def unreachable_claims(self) -> list[ClaimCheck]:
        return [c for c in self.claims if c.status == Status.UNREACHABLE]

    def source_statuses(self) -> dict[int, Status]:
        rank = {
            Status.VERIFIED: 3,
            Status.PARTIAL: 2,
            Status.UNVERIFIED: 1,
            Status.UNREACHABLE: 0,
        }
        best: dict[int, Status] = {}
        for claim in self.claims:
            for ev in claim.citations:
                current = best.get(ev.citation_index)
                if current is None or rank[ev.status] > rank[current]:
                    best[ev.citation_index] = ev.status
        return best

    def used_citation_indices(self) -> list[int]:
        used: set[int] = set()
        for claim in self.claims:
            for ev in claim.citations:
                if ev.status in (Status.VERIFIED, Status.PARTIAL, Status.UNVERIFIED, Status.UNREACHABLE):
                    used.add(ev.citation_index)
        return sorted(used)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "verified_ratio": self.verified_ratio,
            "claims": [
                {
                    "claim": c.claim,
                    "raw_sentence": c.raw_sentence,
                    "status": c.status.value,
                    "note": c.note,
                    "best_score": c.best_score,
                    "citations": [
                        {
                            "index": ev.citation_index,
                            "url": ev.url,
                            "status": ev.status.value,
                            "quote": ev.quote,
                            "score": ev.score,
                            "note": ev.note,
                        }
                        for ev in c.citations
                    ],
                }
                for c in self.claims
            ],
        }


def verify_report(report_text: str, citation_urls: list[str]) -> VerificationReport:
    text = _strip_sources_section(report_text)
    text = _normalize_report_text(text)
    claims = _extract_claims(text)
    if not claims:
        return VerificationReport(claims=[])

    cache: dict[str, PageContent | None] = {}

    for claim in claims:
        evidence_list: list[ClaimEvidence] = []
        for index in claim.citation_indices:
            url = _citation_url(index, citation_urls)
            if not url:
                evidence_list.append(
                    ClaimEvidence(
                        citation_index=index,
                        url="",
                        status=Status.UNVERIFIED,
                        note="Citation index out of range",
                    )
                )
                continue

            if url not in cache:
                cache[url] = _fetch_page_content(url)
            page = cache[url]

            if page is None:
                evidence_list.append(
                    ClaimEvidence(
                        citation_index=index,
                        url=url,
                        status=Status.UNREACHABLE,
                        note="Could not fetch or parse page",
                    )
                )
                continue

            evidence_list.append(_verify_claim_against_page(claim.claim, index, url, page))

        claim.citations = evidence_list
        claim.status = _claim_status(evidence_list)
        claim.best_score = _best_score(evidence_list)
        if claim.status == Status.UNREACHABLE and claim.best_score < _MIN_KEEP_SCORE:
            claim.best_score = _MIN_KEEP_SCORE

    return VerificationReport(claims=claims)


def _strip_sources_section(report_text: str) -> str:
    lower = report_text.lower()
    idx = lower.find("## sources")
    if idx == -1:
        return report_text
    return report_text[:idx]


def _normalize_report_text(text: str) -> str:
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = cleaned.replace("**", "")
    cleaned = re.sub(r"^[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\d+\.\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_claims(text: str) -> list[ClaimCheck]:
    sentences = _SENTENCE_SPLIT.split(text)
    claims: list[ClaimCheck] = []
    for sentence in sentences:
        raw = sentence.strip()
        if not raw:
            continue
        if raw.startswith("[") and "http" in raw.lower():
            continue

        indices = [int(x) for x in _CITATION_RE.findall(raw)]
        if not indices:
            continue

        indices = sorted(set(indices))
        claim_text = _CITATION_RE.sub("", raw).strip()
        claim_text = re.sub(r"\s+", " ", claim_text)
        if len(claim_text) < 15:
            continue

        claims.append(
            ClaimCheck(
                claim=claim_text,
                raw_sentence=raw,
                citation_indices=indices,
            )
        )

    return claims


def _citation_url(index: int, citation_urls: list[str]) -> str:
    if 1 <= index <= len(citation_urls):
        return citation_urls[index - 1]
    return ""


def _fetch_page_content(url: str) -> PageContent | None:
    last_exc: Exception | None = None
    for attempt in range(_FETCH_RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT)
            if resp.status_code == 429:
                wait = _retry_after(resp) or (_RETRY_BACKOFF * (attempt + 1))
                log.warning("Rate limited  url=%s  wait=%.1fs", url, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                log.warning("Access blocked  url=%s  status=%s", url, resp.status_code)
                return None

            resp.raise_for_status()
            if resp.encoding is None:
                resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = " ".join(soup.get_text(separator=" ").split())
            text = text[:_MAX_TEXT_CHARS]
            sentences = _SENTENCE_SPLIT.split(text)
            if len(sentences) > _MAX_SENTENCES:
                sentences = sentences[:_MAX_SENTENCES]
            return PageContent(text=text, sentences=sentences, text_lower=text.lower())
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= _FETCH_RETRIES:
                break
            wait = _RETRY_BACKOFF * (attempt + 1)
            log.warning("Fetch retry  url=%s  wait=%.1fs  reason=%s", url, wait, exc)
            time.sleep(wait)

    log.warning("Fetch failed  url=%s  reason=%s", url, last_exc)
    return None


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _verify_claim_against_page(
    claim: str, index: int, url: str, page: PageContent
) -> ClaimEvidence:
    claim_lc = claim.lower()

    if claim_lc in page.text_lower:
        quote = _find_exact_sentence(claim_lc, page.sentences)
        if not quote:
            quote = _extract_snippet(page.text, claim_lc)
        return ClaimEvidence(
            citation_index=index,
            url=url,
            status=Status.VERIFIED,
            quote=quote,
            score=1.0,
            note="Exact substring match",
        )

    best_sentence = ""
    best_score = 0.0
    best_note = ""

    for sentence in page.sentences:
        if not sentence:
            continue
        token_score = _token_overlap(claim, sentence)
        seq_score = SequenceMatcher(None, claim_lc, sentence.lower()).ratio()
        if token_score >= seq_score:
            score = token_score
            note = "Fuzzy token overlap"
        else:
            score = seq_score
            note = "Fuzzy sequence similarity"

        if score > best_score:
            best_score = score
            best_sentence = sentence
            best_note = note

    if best_score >= _TOKEN_OVERLAP_THRESHOLD or best_score >= _FUZZY_SEQ_THRESHOLD:
        return ClaimEvidence(
            citation_index=index,
            url=url,
            status=Status.PARTIAL,
            quote=best_sentence,
            score=best_score,
            note=best_note,
        )

    return ClaimEvidence(
        citation_index=index,
        url=url,
        status=Status.UNVERIFIED,
        score=best_score,
        note="No supporting sentence found",
    )


def _find_exact_sentence(claim_lc: str, sentences: list[str]) -> str:
    for sentence in sentences:
        if claim_lc in sentence.lower():
            return sentence
    return ""


def _extract_snippet(text: str, claim_lc: str, window: int = 200) -> str:
    idx = text.lower().find(claim_lc)
    if idx == -1:
        return ""
    start = max(idx - window, 0)
    end = min(idx + len(claim_lc) + window, len(text))
    return text[start:end].strip()


def _token_overlap(a: str, b: str) -> float:
    a_tokens = set(_TOKEN_RE.findall(a.lower()))
    if not a_tokens:
        return 0.0
    b_tokens = set(_TOKEN_RE.findall(b.lower()))
    return len(a_tokens & b_tokens) / len(a_tokens)


def _best_score(evidence_list: list[ClaimEvidence]) -> float:
    if not evidence_list:
        return 0.0
    return max((ev.score for ev in evidence_list), default=0.0)


def _claim_status(evidence_list: list[ClaimEvidence]) -> Status:
    if not evidence_list:
        return Status.UNVERIFIED
    if any(ev.status == Status.VERIFIED for ev in evidence_list):
        return Status.VERIFIED
    if any(ev.status == Status.PARTIAL for ev in evidence_list):
        return Status.PARTIAL
    if all(ev.status == Status.UNREACHABLE for ev in evidence_list):
        return Status.UNREACHABLE
    return Status.UNVERIFIED
