# -*- coding: utf-8 -*-
import asyncio
import logging
import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_URL = "https://api.perplexity.ai/chat/completions"
_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")


@dataclass
class ResearchResult:
    content: str
    citations: list[str]
    model: str
    usage: dict


async def run_deep_research(
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: int = 660,
) -> ResearchResult:
    if not _API_KEY:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "sonar-deep-research",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "return_citations": True,
    }

    log.info("Calling Perplexity sonar-deep-research (may take several minutes)")

    timeout = httpx.Timeout(float(timeout_seconds), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await _post_with_retries(client, headers, payload)

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    citations = data.get("citations", [])
    usage = data.get("usage", {})
    model = data.get("model", "sonar-deep-research")

    log.info(
        "Perplexity complete  chars=%d  citations=%d  tokens=%d",
        len(content),
        len(citations),
        usage.get("total_tokens", 0),
    )

    return ResearchResult(content=content, citations=citations, model=model, usage=usage)


async def _post_with_retries(
    client: httpx.AsyncClient,
    headers: dict,
    payload: dict,
    retries: int = 2,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.post(_URL, headers=headers, json=payload)
            if resp.status_code == 401:
                raise RuntimeError("Perplexity API key rejected (401 Unauthorized)")
            if resp.status_code in (429, 500, 502, 503, 504, 529):
                raise httpx.HTTPStatusError(
                    f"Retryable error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.HTTPError, RuntimeError) as exc:
            last_exc = exc
            if isinstance(exc, RuntimeError) and "401" in str(exc):
                raise
            if attempt >= retries:
                break
            wait = 2.0 * (2**attempt)
            log.warning("Perplexity request failed (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(wait)
    raise last_exc or RuntimeError("Perplexity request failed")
