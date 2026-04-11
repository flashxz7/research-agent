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
  Gaps & Uncertainty — plus Sub-Question Coverage and What This Means for Hemut
  where applicable.
- Do NOT include a Sources section — sources will be appended separately.
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


def _extract_and_strip_sources(text: str) -> tuple[str, str]:
    """
    Remove the Sources section from text and return (text_without_sources, sources_block).
    sources_block is a clean markdown string starting with '## Sources\\n'.
    """
    pat = re.compile(r'\n(#{2,3}\s+Sources\b[^\n]*\n)([\s\S]*?)(?=\n#{2,3}\s+|\Z)')
    m = pat.search(text)
    if m:
        heading = m.group(1).strip()
        body = m.group(2).strip()
        stripped = (text[:m.start()] + text[m.end():]).strip()
        sources_block = f"{heading}\n{body}" if body else heading
        return stripped, sources_block
    return text.strip(), ""


def _build_sources_block(citation_urls: list[str], verification: VerificationReport) -> str:
    """Build a clean Sources section with all cited URLs as [N] url lines."""
    lines = ["## Sources", ""]
    used_indices = verification.used_citation_indices()
    indices_to_use = used_indices if used_indices else list(range(1, len(citation_urls) + 1))
    for i in indices_to_use:
        if 1 <= i <= len(citation_urls):
            lines.append(f"[{i}] {citation_urls[i - 1]}")
    return "\n".join(lines)


async def format_digest(
    report_text: str,
    verification: VerificationReport,
    citation_urls: list[str],
    classification: dict,
    raw_report: str | None = None,
) -> str:
    output_format = classification.get("output_format", "narrative")
    depth = classification.get("depth", "deep")

    # Always build the authoritative sources block from citation_urls
    sources_block = _build_sources_block(citation_urls, verification)

    if _client is None:
        return f"{report_text}\n\n{sources_block}"

    # Strip any existing Sources section before sending to GPT-4o so it can't
    # scatter, duplicate, or reformat them
    report_for_gpt, _ = _extract_and_strip_sources(report_text)
    # Also strip Verification Notes footer
    report_for_gpt = re.sub(r'\n?---\s*\n+#{1,3}\s+Verification Notes[\s\S]*$', '', report_for_gpt).strip()

    format_hint = _format_hint(output_format)
    user_content = (
        f"Intended output format: {output_format}. {format_hint}\n\n"
        "Reformat the report below into a complete, readable answer.\n"
        "Use only the claims contained in the input report.\n"
        "If the input contains [UNVERIFIED] tags, keep the claim but add a brief\n"
        "caution phrase such as 'could not be independently confirmed'.\n"
        "Do NOT include a Sources section — it will be appended automatically.\n\n"
        f"{report_for_gpt}"
    )

    response = await _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _FORMAT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=16000,
    )

    formatted = (response.choices[0].message.content or "").strip()
    if not formatted:
        formatted = report_for_gpt

    # Strip any Sources section GPT-4o may have added despite instructions
    formatted, _ = _extract_and_strip_sources(formatted)

    if depth == "deep" and len(formatted.split()) < _MIN_DEEP_WORDS:
        log.warning("Formatted output too short for deep request; returning raw report")
        fallback = raw_report or report_text
        fallback, _ = _extract_and_strip_sources(fallback)
        fallback = re.sub(r'\n?---\s*\n+#{1,3}\s+Verification Notes[\s\S]*$', '', fallback).strip()
        return f"{fallback}\n\n{sources_block}"

    return f"{formatted}\n\n{sources_block}"


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


async def compress_to_concise(full_digest: str) -> str:
    """
    Takes the full research digest and compresses it to the 3-5 most important
    findings in 300-400 words. The full digest is stored separately for follow-up context.
    """
    if _client is None:
        words = full_digest.split()
        return " ".join(words[:400]) + "\n\n*[Full research stored for follow-up questions]*"

    system = """\
You are a research editor. Your job is to compress a full research report into a
concise summary for a busy executive.

Rules:
- Extract only the 3-5 most important findings from the full report.
- Total output must be 300-400 words maximum.
- Each finding gets one short paragraph of 2-3 sentences.
- Preserve all inline citation markers [N] exactly as they appear in the source.
- Use ## headings for each finding.
- End with a one-sentence ## Bottom Line that states the single most actionable insight.
- Do not add any information not present in the source report.
- Do not include a Sources section — citations are inline only.
- Output raw Markdown only. No preamble, no meta-commentary.
"""

    user = (
        "Compress this research report to the 3-5 most important findings "
        "in 300-400 words:\n\n" + full_digest
    )

    try:
        response = await _client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        compressed = (response.choices[0].message.content or "").strip()
        if not compressed:
            return full_digest
        return compressed
    except Exception as exc:
        log.error("Concise compression failed: %s", exc)
        return full_digest


async def compress_to_list(full_digest: str) -> str:
    """
    Takes the full research digest and formats it as structured bullet points.
    Designed for planning and workflow breakdown use cases.
    The full digest is stored separately for follow-up context.
    """
    if _client is None:
        words = full_digest.split()
        return " ".join(words[:400]) + "\n\n*[Full research stored for follow-up questions]*"

    system = """\
You are a research editor. Your job is to convert a full research report into a
structured bullet-point breakdown optimized for planning and workflow decisions.

Rules:
- Organize bullets under 3-4 ## headings that reflect actionable categories
  (e.g. ## Key Facts, ## Trends to Watch, ## Implications, ## Open Questions).
- Each bullet is one specific, concrete fact or insight — one sentence maximum.
- Total bullets: 10-15. Each bullet must have an inline citation [N] if one exists
  in the source for that fact.
- No prose paragraphs between bullets. Headings and bullets only.
- Do not add any information not present in the source report.
- Do not include a Sources section.
- Output raw Markdown only. No preamble, no meta-commentary.
"""

    user = (
        "Convert this research report into a structured bullet-point breakdown "
        "for planning and workflow decisions:\n\n" + full_digest
    )

    try:
        response = await _client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=800,
        )
        compressed = (response.choices[0].message.content or "").strip()
        if not compressed:
            return full_digest
        return compressed
    except Exception as exc:
        log.error("List compression failed: %s", exc)
        return full_digest
