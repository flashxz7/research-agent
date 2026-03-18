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
    log.info("Pipeline start  issue=%s  title=%r  mode=%s", issue_id, title, research_mode)

    normalized_mode = (research_mode or "extensive").strip().lower()
    if normalized_mode not in {"extensive", "concise", "list"}:
        log.warning("Unknown research mode %r; defaulting to extensive", research_mode)
        normalized_mode = "extensive"

    # Step 1 — Classify
    log.info("Step 1/7: Classifying issue  issue=%s", issue_id)
    classification = await classify_issue(title, description)
    log.info("Step 1/7 complete  issue=%s  intent=%s", issue_id, classification.get("intent"))

    # Step 2 — Build prompts
    log.info("Step 2/7: Building prompts  issue=%s", issue_id)
    system_prompt, user_prompt = build_dynamic_prompt(classification, title, description)
    log.info("Step 2/7 complete  issue=%s  prompt_chars=%d", issue_id, len(user_prompt))

    timeout_seconds = 660

    # Step 3 — Perplexity deep research
    log.info("Step 3/7: Calling Perplexity sonar-deep-research  issue=%s  timeout=%ds", issue_id, timeout_seconds)
    try:
        result = await run_deep_research(
            system_prompt,
            user_prompt,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        log.error("Step 3/7 FAILED  issue=%s  error=%s", issue_id, exc)
        message = f"Research could not be completed.\n\n`{exc}`"
        if post_to_linear and linear_enabled():
            await post_comment(issue_id, message)
        return message

    log.info(
        "Step 3/7 complete  issue=%s  chars=%d  citations=%d  tokens=%d",
        issue_id,
        len(result.content),
        len(result.citations),
        result.usage.get("total_tokens", 0),
    )

    word_count = len(result.content.split())
    if normalized_mode == "extensive":
        if word_count < 2500:
            log.warning("Output below minimum word count: %d words", word_count)
        if len(result.citations) < 12:
            log.warning("Output below minimum citations: %d sources", len(result.citations))

    # Step 4 — Verification
    log.info("Step 4/7: Running claim verification  issue=%s  citations=%d", issue_id, len(result.citations))
    verification = await asyncio.to_thread(verify_report, result.content, result.citations)
    log.info(
        "Step 4/7 complete  issue=%s  summary=%s",
        issue_id,
        verification.summary or "no claims found",
    )

    # Step 5 — Build verified report
    log.info("Step 5/7: Building verified report  issue=%s", issue_id)
    intent = classification.get("intent", "market_research")
    if intent == "code_debug":
        base_report = result.content
        log.info("Step 5/7 complete  issue=%s  using raw content (code_debug)", issue_id)
    else:
        base_report = build_verified_report(
            title=title,
            verification=verification,
            citation_urls=result.citations,
            classification=classification,
        )
        log.info("Step 5/7 complete  issue=%s  report_chars=%d", issue_id, len(base_report))

    # Step 6 — GPT-4o formatting
    log.info("Step 6/7: Formatting digest with GPT-4o  issue=%s", issue_id)
    try:
        digest = await format_digest(
            report_text=base_report,
            verification=verification,
            citation_urls=result.citations,
            classification=classification,
            raw_report=result.content,
        )
        log.info("Step 6/7 complete  issue=%s  digest_chars=%d", issue_id, len(digest))
    except Exception as exc:
        log.error("Step 6/7 FAILED  issue=%s  error=%s", issue_id, exc)
        digest = base_report

    # Step 6b — Compress for concise/list modes
    full_digest = digest
    if normalized_mode == "concise":
        log.info("Step 6b/7: Compressing to concise format  issue=%s", issue_id)
        try:
            digest = await compress_to_concise(full_digest)
            log.info("Step 6b/7 complete  issue=%s  compressed_chars=%d", issue_id, len(digest))
        except Exception as exc:
            log.error("Step 6b/7 FAILED  issue=%s  error=%s", issue_id, exc)
            digest = full_digest
    elif normalized_mode == "list":
        log.info("Step 6b/7: Compressing to list format  issue=%s", issue_id)
        try:
            digest = await compress_to_list(full_digest)
            log.info("Step 6b/7 complete  issue=%s  compressed_chars=%d", issue_id, len(digest))
        except Exception as exc:
            log.error("Step 6b/7 FAILED  issue=%s  error=%s", issue_id, exc)
            digest = full_digest

    # Step 7 — Save artifact and post to Linear
    log.info("Step 7/7: Saving artifact  issue=%s", issue_id)
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
    log.info("Step 7/7 artifact saved  issue=%s", issue_id)

    if post_to_linear and linear_enabled():
        log.info("Step 7/7: Posting to Linear  issue=%s", issue_id)
        posted = await post_comment(issue_id, digest)
        if posted:
            log.info("Step 7/7 complete  issue=%s  posted to Linear successfully", issue_id)
        else:
            log.error("Step 7/7 FAILED  issue=%s  Linear post returned false", issue_id)
    else:
        log.info("Step 7/7 complete  issue=%s  skipping Linear post", issue_id)

    log.info("Pipeline complete  issue=%s  mode=%s  final_chars=%d", issue_id, normalized_mode, len(digest))
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