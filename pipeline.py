# -*- coding: utf-8 -*-
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from formats import (
    build_verified_report,
    compress_to_concise,
    compress_to_list,
    format_digest,
)
from linear_client import is_enabled as linear_enabled
from linear_client import post_comment
from perplexity_client import run_deep_research
from prompts import build_dynamic_prompt, classify_issue
from verification import verify_report

log = logging.getLogger(__name__)

_ARTIFACTS = Path("artifacts")


async def run_research_pipeline(
    issue_id: str,
    title: str,
    description: str,
    post_to_linear: bool = True,
    research_mode: str = "extensive",
) -> str:
    log.info("Pipeline start  issue=%s  title=%r", issue_id, title)
    normalized_mode = (research_mode or "extensive").strip().lower()
    if normalized_mode not in {"extensive", "concise", "list"}:
        log.warning("Unknown research mode %r; defaulting to extensive", research_mode)
        normalized_mode = "extensive"

    classification = await classify_issue(title, description)
    log.info("Classification  issue=%s  %s", issue_id, classification)

    system_prompt, user_prompt = build_dynamic_prompt(classification, title, description)
    timeout_seconds = 660

    try:
        result = await run_deep_research(
            system_prompt,
            user_prompt,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        log.error("Perplexity failed  issue=%s  error=%s", issue_id, exc)
        message = f"Research could not be completed.\n\n`{exc}`"
        if post_to_linear and linear_enabled():
            await post_comment(issue_id, message)
        return message

    word_count = len(result.content.split())
    if word_count < 2500:
        log.warning(
            "Perplexity output below minimum word count: %d words", word_count
        )
    if len(result.citations) < 12:
        log.warning(
            "Perplexity output below minimum citations: %d sources", len(result.citations)
        )

    verification = await asyncio.to_thread(verify_report, result.content, result.citations)
    log.info("Verification complete  %s", verification.summary or "no claims found")

    intent = classification.get("intent", "market_research")
    if intent == "code_debug":
        base_report = result.content
    else:
        base_report = build_verified_report(
            title=title,
            verification=verification,
            citation_urls=result.citations,
            classification=classification,
        )

    try:
        digest = await format_digest(
            report_text=base_report,
            verification=verification,
            citation_urls=result.citations,
            classification=classification,
            raw_report=result.content,
        )
    except Exception as exc:
        log.error("Formatting failed  issue=%s  error=%s", issue_id, exc)
        digest = base_report

    full_digest = digest
    if normalized_mode == "concise":
        log.info("Compressing to concise format  issue=%s", issue_id)
        try:
            digest = await compress_to_concise(full_digest)
        except Exception as exc:
            log.error("Concise compression failed  issue=%s  error=%s", issue_id, exc)
            digest = full_digest
    elif normalized_mode == "list":
        log.info("Compressing to list format  issue=%s", issue_id)
        try:
            digest = await compress_to_list(full_digest)
        except Exception as exc:
            log.error("List compression failed  issue=%s  error=%s", issue_id, exc)
            digest = full_digest

    _save_artifact(
        issue_id,
        title,
        result,
        verification,
        base_report,
        full_digest,
        digest,
        classification,
        normalized_mode,
    )

    if post_to_linear and linear_enabled():
        posted = await post_comment(issue_id, digest)
        if posted:
            log.info("Digest posted  issue=%s", issue_id)
        else:
            log.error("Failed to post comment  issue=%s", issue_id)
    else:
        log.info("Skipping Linear post for issue=%s", issue_id)

    return digest


def _save_artifact(
    issue_id: str,
    title: str,
    result,
    verification,
    report: str,
    full_digest: str,
    digest: str,
    classification: dict,
    research_mode: str,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _ARTIFACTS / f"{ts}_{issue_id}.json"
    payload = {
        "issue_id": issue_id,
        "title": title,
        "model": result.model,
        "usage": result.usage,
        "citations": result.citations,
        "content": result.content,
        "classification": classification,
        "research_mode": research_mode,
        "verification": verification.to_dict() if verification else None,
        "verified_report": report,
        "full_digest": full_digest,
        "digest": digest,
    }
    path.write_text(json.dumps(payload, indent=2))
    log.info("Artifact saved  path=%s", path)
