# -*- coding: utf-8 -*-
import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_URL = "https://api.linear.app/graphql"
_API_KEY = os.getenv("LINEAR_API_KEY", "")
_HEADERS = {"Authorization": _API_KEY, "Content-Type": "application/json"}


def is_enabled() -> bool:
    return bool(_API_KEY)


async def get_issue_labels(issue_id: str) -> list[str]:
    if not _API_KEY:
        log.warning("LINEAR_API_KEY not set; skipping label fetch")
        return []

    query = """
    query IssueLabels($id: String!) {
        issue(id: $id) {
            labels { nodes { name } }
        }
    }
    """
    payload = {"query": query, "variables": {"id": issue_id}}
    resp = await _post_with_retries(payload)

    nodes = (
        resp
        .get("data", {})
        .get("issue", {})
        .get("labels", {})
        .get("nodes", [])
    )
    return [n["name"].lower() for n in nodes]


async def post_comment(issue_id: str, body: str) -> bool:
    if not _API_KEY:
        log.warning("LINEAR_API_KEY not set; skipping comment post")
        return False

    mutation = """
    mutation CommentCreate($issueId: String!, $body: String!) {
        commentCreate(input: { issueId: $issueId, body: $body }) {
            success
        }
    }
    """
    payload = {"query": mutation, "variables": {"issueId": issue_id, "body": body}}
    resp = await _post_with_retries(payload)

    success = resp.get("data", {}).get("commentCreate", {}).get("success", False)
    if not success:
        log.error("Linear commentCreate returned success=false  issue=%s", issue_id)
    return success


async def _post_with_retries(
    payload: dict,
    retries: int = 2,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(_URL, headers=_HEADERS, json=payload)

            if resp.status_code == 401:
                raise RuntimeError("Linear API key rejected (401 Unauthorized)")
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"Retryable error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()

            data = resp.json()
            if data.get("errors"):
                log.error("Linear errors: %s", data["errors"])
            return data
        except (httpx.TimeoutException, httpx.HTTPError, RuntimeError) as exc:
            last_exc = exc
            if isinstance(exc, RuntimeError) and "401" in str(exc):
                raise
            if attempt >= retries:
                break
            wait = 2.0 * (2**attempt)
            log.warning("Linear request failed (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(wait)
    raise last_exc or RuntimeError("Linear request failed")
