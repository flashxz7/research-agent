# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

log = logging.getLogger(__name__)

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
_client = AsyncOpenAI(api_key=_OPENAI_API_KEY) if _OPENAI_API_KEY else None

_ALLOWED_INTENTS = {
    "market_research",
    "competitive_analysis",
    "regulatory_question",
    "technical_explainer",
    "strategic_question",
    "vendor_evaluation",
    "idea_validation",
    "code_debug",
    "definition",
}

_ALLOWED_DEPTH = {"shallow", "deep"}

_ALLOWED_AUDIENCE = {"executive", "operator", "engineer", "general"}

_ALLOWED_OUTPUT_FORMAT = {
    "narrative",
    "comparison_table",
    "step_by_step",
    "regulatory_summary",
    "definition_block",
}

_BASE_SYSTEM_PROMPT = """\
You are a precise research analyst embedded in Hemut's operations. Hemut is a
YC-backed trucking TMS (Transportation Management System) startup serving mid-market
fleets and small owner-operators in the United States.

CITATION REQUIREMENT — THIS IS NON-NEGOTIABLE:
Every sentence that contains a fact, statistic, claim, date, company name used in
 a factual context, or any assertion that could be disputed must end with at least
 one inline citation in the format [N]. A sentence without a citation is either an
 opinion or a hallucination — label opinions explicitly as "In the author's assessment"
 and eliminate hallucinations entirely. If you write three consecutive sentences
 without a citation marker you have violated this requirement. Before submitting
 your response, scan every sentence and verify each one has a citation or is
 explicitly marked as an opinion.

Minimum depth requirements for deep research:
- Minimum 2,500 words in the raw output before verification.
- Minimum 12 distinct sources in the Sources section.
- Executive Summary must have at least 3 cited sentences.
- Each Key Finding must contain at least 4 cited claims and at least one specific
  data point with a unit (percentage, dollar figure, company name, date, or count).
- Each Key Finding must be at least 150 words.
- Supporting Detail must contain at least 8 cited claims in total.

A shallow, surface-level response that could be produced by a basic web search is
unacceptable. Your output must contain specific named companies, specific sourced
figures, and specific dated events that demonstrate genuine deep research across
multiple sources.

Hemut context filter:
Hemut is a software company — a TMS platform vendor. It does not own trucks, employ
 drivers, or operate freight lanes. Any content about driver recruitment, equipment
 purchasing, fuel costs, or carrier operations is only relevant if it explains how
 those factors affect Hemut's software customers and therefore Hemut's product or
 business. Never include operational advice designed for a trucking carrier in a
 report for a software vendor.

Non-negotiable rules:
- Do not speculate or infer beyond what sources explicitly state.
- Do not use vague language like "roughly" or "estimated" without a source.
- Prefer primary sources over secondary summaries whenever possible.
- Use direct quotes or close paraphrases for key claims to aid verification.
- If sources conflict, present both positions and note the conflict.
- Answer every explicit sub-question in the issue. If you cannot find sourced
  information for a specific sub-question, say so explicitly in Gaps & Uncertainty.
- Avoid generic restatements; prioritize specific, non-obvious details, dates,
  numbers, named entities, and concrete examples.
- If the question requests examples or failures, you must name specific companies
  and cite sources. If you cannot find named examples, say so explicitly.
- Do not include a recommendations, strategies, or action items section. Your job
  is to report research findings accurately and completely.

The only sections permitted are: Executive Summary, Key Findings, Supporting Detail,
Gaps & Uncertainty, Sources — plus Sub-Question Coverage and What This Means for Hemut
where applicable.
"""

_HEMUT_SECTION = """\
After your Sources section, add a final section titled "## What This Means for Hemut".
Connect findings directly to Hemut's position as a TMS vendor targeting mid-market
fleets and owner-operators. Be specific: name which Hemut product this affects,
whether this is a threat, opportunity, or integration candidate, and a concrete
next step. Do not write generic strategic fluff.

Additionally, after each Key Finding include a 1–2 sentence line labeled
"Hemut implication:" that connects that finding to Hemut's product or business.
"""

_INTENT_BLOCKS = {
    "market_research": """\
Coverage must include:
- Market size with CAGR and forecast years.
- Top 3-5 players with revenue or funding if available.
- Adoption rate among trucking fleets specifically (not general logistics).
- One specific ROI or cost-impact data point for fleets.
- One concrete adoption barrier for fleets under 200 trucks.
Use a compact table only if it clarifies the market sizing.
""",
    "competitive_analysis": """\
Coverage must include:
- Named vendors with: pricing model, key differentiator, target fleet size, and
  known weaknesses.
- A recommendation stating which option fits which fleet profile.
Do not write generic "each has pros and cons"; take a position on each vendor.
Use a table only if it improves clarity, otherwise keep it narrative.
""",
    "regulatory_question": """\
Coverage must include:
- Exact rule/statute name and number.
- Effective date.
- Who it applies to.
- What specifically changes for owner-operators vs large fleets.
- Current enforcement posture from FMCSA or DOT.
- Pending amendments or proposed changes.
Lead with a plain-language TL;DR before citations.
""",
    "technical_explainer": """\
Coverage must include:
- Plain-language explanation first.
- Technical depth second.
- One concrete example.
- Common misconceptions.
Use steps only if it is a process; otherwise keep it narrative.
""",
    "strategic_question": """\
Coverage must include:
- You must name and analyze at least five specific companies relevant to this
  question. For each named company provide: what they did, what happened, why it
  succeeded or failed, and the direct lesson.
- At least 6 specific logistics/trucking/TMS startups that failed or pivoted
  (2015-2026) with documented failure/pivot reasons.
- At least 2 YC logistics/trucking companies if they can be sourced; if none can
  be confirmed, say so explicitly.
- Failure modes, mechanisms, and early warning signals tied to named companies.
- What successful companies did differently with concrete actions.
- A Hemut risk map (12-18 months) mapping failure modes to product/ops with
  severity (High/Medium/Low) and rationale.
Keep this narrative and insight-driven; use short subheadings or bullets only
when they improve clarity.
""",
    "vendor_evaluation": """\
Coverage must include for each vendor:
- Pricing model.
- Integration complexity.
- Trucking-specific features.
- Customer reviews from trucking operators (not generic SaaS reviews).
- Known failure modes.
Provide a ranked recommendation. Use a table only if it improves clarity.
""",
    "idea_validation": """\
You must name and analyze at least five specific companies relevant to this
question. For each named company provide: what they did, what happened, why it
succeeded or failed, and the direct lesson. Generic industry statistics are
supplementary — named company case studies are the primary content requirement.
If you cannot find five named examples, report that explicitly in Gaps & Uncertainty.

Coverage must include:
- Prior art (has this been tried before and what happened).
- Market demand signals.
- Technical or operational feasibility.
- Risks specific to trucking.
- One analogous success case from an adjacent industry.
Keep it narrative and evidence-driven.
""",
    "code_debug": """\
Coverage must include:
- Exact diagnosis of the error.
- Root cause explanation.
- Fix with code snippet if applicable.
- How to prevent recurrence.
Be detailed and explicit. Keep the response practical and implementation-ready.
""",
    "definition": """\
Coverage must include:
- Plain-language definition in the first sentence.
- Technical definition second.
- How it applies specifically in trucking operations.
- One real example.
Keep it concise but clear.
""",
}

_CLASSIFIER_SYSTEM_PROMPT = """\
You are a research intent classifier for a YC-backed trucking TMS startup.
Return ONLY valid JSON matching this schema exactly:
{
  "intent": "one of: market_research | competitive_analysis | regulatory_question | technical_explainer | strategic_question | vendor_evaluation | idea_validation | code_debug | definition",
  "depth": "one of: shallow | deep",
  "audience": "one of: executive | operator | engineer | general",
  "hemut_context": true or false,
  "research_angle": "one precise sentence describing what to research and from what angle",
  "output_format": "one of: narrative | comparison_table | step_by_step | regulatory_summary | definition_block",
  "extracted_sub_questions": ["string", "string"]
}
Rules:
- hemut_context is false for code_debug, definition, and technical_explainer. True for all other intents.
- The research_angle must reference every sub-question in the original issue description.
  If the issue asks about YC companies, the research_angle must explicitly say
  "find named YC-backed logistics companies and their outcomes." Never produce a
  research angle so generic that it could apply to any company in the industry.
- Return ONLY JSON. No commentary.
"""

_SUBQ_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+(.*)")


def base_system_prompt() -> str:
    return _BASE_SYSTEM_PROMPT


async def classify_issue(title: str, description: str) -> dict[str, Any]:
    if _client is None:
        return _fallback_classification(title, description)

    payload = {
        "title": title.strip(),
        "description": description.strip(),
    }

    try:
        response = await _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=350,
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return _normalize_classification(data, title, description)
    except Exception as exc:
        log.warning("Classifier failed; falling back. error=%s", exc)
        return _fallback_classification(title, description)


def build_dynamic_prompt(
    classification: dict[str, Any], title: str, description: str
) -> tuple[str, str]:
    intent = classification.get("intent", "market_research")
    intent_block = _INTENT_BLOCKS.get(intent, "")

    system_prompt = f"{_BASE_SYSTEM_PROMPT}\n\n{intent_block}".strip()
    if classification.get("hemut_context"):
        system_prompt = f"{system_prompt}\n\n{_HEMUT_SECTION}".strip()

    research_angle = (classification.get("research_angle") or title or "").strip()
    if not research_angle:
        research_angle = "Research the issue and provide a clear answer."

    subqs = classification.get("extracted_sub_questions") or []
    subq_block = ""
    if subqs:
        rendered = "\n".join(f"- {q}" for q in subqs)
        subq_block = (
            "The original issue description contains the following specific sub-questions:\n"
            f"{rendered}\n\n"
            "You must address each one individually. After your Sources section and before\n"
            "What This Means for Hemut, add a section titled '## Sub-Question Coverage' that\n"
            "lists each sub-question and one sentence stating where in the report it was\n"
            "addressed or explicitly stating it could not be answered with available sources.\n\n"
        )

    user_prompt = (
        f"Research angle: {research_angle}\n\n"
        f"Original issue title: {title.strip()}\n\n"
        f"Additional context: {description.strip() or 'None'}\n\n"
        f"{subq_block}"
        "Answer every explicit sub-question in the issue. "
        "Produce a complete research report following your instructions exactly. "
        "Cite every factual claim inline."
    )

    return system_prompt, user_prompt


def _normalize_classification(
    data: dict[str, Any], title: str, description: str
) -> dict[str, Any]:
    intent = data.get("intent") if data else None
    if intent not in _ALLOWED_INTENTS:
        return _fallback_classification(title, description)

    depth = data.get("depth") if data else None
    audience = data.get("audience") if data else None
    output_format = data.get("output_format") if data else None
    research_angle = data.get("research_angle") if data else None
    sub_questions = data.get("extracted_sub_questions") if data else None

    if depth not in _ALLOWED_DEPTH:
        depth = "deep"
    if audience not in _ALLOWED_AUDIENCE:
        audience = "general"
    if output_format not in _ALLOWED_OUTPUT_FORMAT:
        output_format = _default_output_format(intent)
    if not research_angle:
        research_angle = title.strip() or "Research the issue and provide a clear answer."

    hemut_context = bool(data.get("hemut_context", True))
    if intent in {"code_debug", "definition", "technical_explainer"}:
        hemut_context = False

    extracted = _extract_sub_questions(description)
    if isinstance(sub_questions, list):
        extracted = _merge_unique(sub_questions, extracted)

    return {
        "intent": intent,
        "depth": "deep",
        "audience": audience,
        "hemut_context": hemut_context,
        "research_angle": research_angle,
        "output_format": output_format,
        "extracted_sub_questions": extracted,
    }


def _fallback_classification(title: str, description: str) -> dict[str, Any]:
    text = f"{title} {description}".lower()

    if any(k in text for k in ["traceback", "exception", "error", "stack", "bug"]):
        intent = "code_debug"
    elif any(k in text for k in ["define", "what is", "meaning of"]):
        intent = "definition"
    elif any(k in text for k in ["fmcsa", "dot", "regulation", "law", "compliance"]):
        intent = "regulatory_question"
    elif any(k in text for k in ["compare", "vs", "alternatives", "vendors", "pricing"]):
        intent = "vendor_evaluation"
    elif any(k in text for k in ["market", "trend", "forecast", "tam", "cagr"]):
        intent = "market_research"
    elif any(k in text for k in ["strategy", "should we", "build", "opportunity"]):
        intent = "strategic_question"
    elif any(k in text for k in ["how does", "explain", "architecture", "pipeline"]):
        intent = "technical_explainer"
    else:
        intent = "market_research"

    output_format = _default_output_format(intent)
    hemut_context = intent not in {"code_debug", "definition", "technical_explainer"}

    return {
        "intent": intent,
        "depth": "deep",
        "audience": "general",
        "hemut_context": hemut_context,
        "research_angle": title.strip() or "Research the issue and provide a clear answer.",
        "output_format": output_format,
        "extracted_sub_questions": _extract_sub_questions(description),
    }


def _default_output_format(intent: str) -> str:
    mapping = {
        "market_research": "narrative",
        "competitive_analysis": "comparison_table",
        "regulatory_question": "regulatory_summary",
        "technical_explainer": "narrative",
        "strategic_question": "narrative",
        "vendor_evaluation": "comparison_table",
        "idea_validation": "narrative",
        "code_debug": "step_by_step",
        "definition": "definition_block",
    }
    return mapping.get(intent, "narrative")


def _extract_sub_questions(description: str) -> list[str]:
    if not description:
        return []
    results: list[str] = []
    for line in description.splitlines():
        match = _SUBQ_PATTERN.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                results.append(value)
    return results


def _merge_unique(primary: list[Any], secondary: list[Any]) -> list[str]:
    merged: list[str] = []
    for item in primary + secondary:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in merged:
            merged.append(value)
    return merged
