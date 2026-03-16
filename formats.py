# -*- coding: utf-8 -*-
import logging
import os
import re

from dotenv import load_dotenv
from openai import AsyncOpenAI

from verification import Status, VerificationReport

load_dotenv()

log = logging.getLogger(__name__)

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
_client = AsyncOpenAI(api_key=_OPENAI_API_KEY) if _OPENAI_API_KEY else None

_MIN_KEEP_SCORE = 0.25
_MIN_DEEP_WORDS = 800

_FORMAT_SYSTEM = """\
You are a strict document formatter. Your sole job is to take the report provided
and reformat it into clean, readable Markdown.

Non-negotiable rules:
- Do not add, remove, infer, or change any factual claim under any circumstance.
- Do not insert any information that is not already present in the input.
- Preserve every inline citation marker exactly as written - [1], [2], etc.
- Improve readability: fix awkward phrasing, improve sentence flow.
- Keep the output comprehensive and decision-ready, not generic.
- Do not add recommendations or action items.
- The only sections permitted are: Executive Summary, Key Findings, Supporting Detail,
  Gaps & Uncertainty, Sources — plus Sub-Question Coverage and What This Means for Hemut
  where applicable.
- Avoid rigid templates; use headings only when they add clarity.
- Output raw Markdown only. No preamble, no meta-commentary, no closing note.
"""


def build_verified_report(
    title: str,
    verification: VerificationReport,
    citation_urls: list[str],
    classification: dict,
) -> str:
    subqs = classification.get("extracted_sub_questions") or []

    lines: list[str] = []
    lines.append(f"# {title.strip()}")
    lines.append("")
    if subqs:
        lines.append("## Sub-Questions to Address")
        for q in subqs:
            lines.append(f"- {q}")
        lines.append("")

    lines.append("## Claim Ledger")
    lines.append(
        "Each line below is a sourced sentence from the research draft. "
        "Tags indicate verification status."
    )

    for claim in verification.claims:
        if claim.best_score < _MIN_KEEP_SCORE:
            continue
        sentence = _clean_sentence(claim.raw_sentence)
        tag = _status_tag(claim.status)
        if tag:
            lines.append(f"- {sentence} {tag}")
        else:
            lines.append(f"- {sentence}")

    lines.append("")
    lines.append("## Gaps & Uncertainty")
    lines.extend(_build_gap_notes(verification))

    lines.append("")
    lines.append("## Sources")
    used_indices = verification.used_citation_indices()
    if used_indices:
        for i in used_indices:
            if 1 <= i <= len(citation_urls):
                lines.append(f"[{i}] {citation_urls[i - 1]}")
    else:
        for i, url in enumerate(citation_urls, start=1):
            lines.append(f"[{i}] {url}")

    return "\n".join(lines).strip()


async def format_digest(
    report_text: str,
    verification: VerificationReport,
    citation_urls: list[str],
    classification: dict,
    raw_report: str | None = None,
) -> str:
    output_format = classification.get("output_format", "narrative")
    depth = classification.get("depth", "deep")

    if _client is None:
        footer = build_verification_footer(verification, citation_urls)
        return f"{report_text}\n\n---\n\n{footer}"

    format_hint = _format_hint(output_format)
    user_content = (
        f"Intended output format: {output_format}. {format_hint}\n\n"
        "Reformat the report below into a complete, readable answer.\n"
        "Use only the claims contained in the input report.\n"
        "If the input contains [UNVERIFIED] tags, keep the claim but add a brief\n"
        "caution phrase such as 'could not be independently confirmed'.\n\n"
        f"{report_text}"
    )

    response = await _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _FORMAT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=4096,
    )

    formatted = (response.choices[0].message.content or "").strip()
    if not formatted:
        formatted = report_text.strip()

    if depth == "deep" and len(formatted.split()) < _MIN_DEEP_WORDS:
        log.warning("Formatted output too short for deep request; returning raw report")
        fallback = raw_report or report_text
        footer = build_verification_footer(verification, citation_urls)
        return f"{fallback}\n\n---\n\n{footer}"

    footer = build_verification_footer(verification, citation_urls)
    return f"{formatted}\n\n---\n\n{footer}"


def build_verification_footer(
    verification: VerificationReport, citation_urls: list[str]
) -> str:
    total = len(verification.claims)
    verified = len(verification.verified_claims)
    unverified = len(verification.unverified_claims)
    unreachable = len(verification.unreachable_claims)
    coverage = f"{verification.verified_ratio:.0%}" if total else "0%"

    lines = [
        "### Verification Notes",
        f"Claims verified: {verified}/{total} ({coverage}).",
        f"Claims not verified by the fetcher: {unverified}.",
        f"Claims blocked by unreachable sources: {unreachable}.",
        "",
        "Verification is conservative; unverified does not automatically mean false.",
        "Full verification detail is saved in the artifacts JSON.",
    ]

    unreachable_urls = _unreachable_urls(verification, citation_urls)
    if unreachable_urls:
        lines.append("")
        lines.append("Unreachable sources (sample):")
        for url in unreachable_urls[:5]:
            lines.append(f"- {url}")

    return "\n".join(lines)


def _clean_sentence(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^#+\s+", "", cleaned)
    cleaned = cleaned.replace("**", "")
    cleaned = re.sub(r"\[(\d+)\](\s*\[\1\])+$", r"[\1]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _status_tag(status: Status) -> str:
    if status == Status.UNVERIFIED:
        return "[UNVERIFIED]"
    if status == Status.PARTIAL:
        return "[PARTIAL]"
    if status == Status.UNREACHABLE:
        return "[SOURCE_UNREACHABLE]"
    return ""


def _build_gap_notes(verification: VerificationReport) -> list[str]:
    notes: list[str] = []
    if not verification.claims:
        return ["No claims were extracted from the source report for verification."]

    if verification.unverified_claims:
        notes.append(
            "Some source-cited statements could not be verified and are marked as such."
        )
    if verification.unreachable_claims:
        notes.append(
            "Some sources could not be fetched for verification, which reduced coverage."
        )
    if not notes:
        notes.append("No major evidence gaps were detected in this run.")
    return notes


def _unreachable_urls(
    verification: VerificationReport, citation_urls: list[str]
) -> list[str]:
    statuses = verification.source_statuses()
    urls: list[str] = []
    for index, status in statuses.items():
        if status == Status.UNREACHABLE and 1 <= index <= len(citation_urls):
            urls.append(citation_urls[index - 1])
    return urls


def _format_hint(output_format: str) -> str:
    if output_format == "comparison_table":
        return "Use a comparison table only if it improves clarity."
    if output_format == "regulatory_summary":
        return "Lead with a plain-language TL;DR before any citations."
    if output_format == "step_by_step":
        return "Use numbered steps only when it improves comprehension."
    if output_format == "definition_block":
        return "Keep it concise and definition-focused with a clear example."
    return "Use a clear narrative structure with minimal headings."
