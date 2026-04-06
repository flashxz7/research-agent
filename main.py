# -*- coding: utf-8 -*-
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from linear_client import get_issue_labels
from pipeline import PipelineResult, run_research_pipeline

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

WEBHOOK_SECRET = os.getenv("LINEAR_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
RESEARCH_LABEL = "research-agent"
_active_jobs: set[str] = set()
_recent_jobs: dict[str, float] = {}
_RECENT_TTL_SECONDS = 600


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("artifacts", exist_ok=True)
    yield


os.makedirs("artifacts", exist_ok=True)
app = FastAPI(lifespan=lifespan)
app.mount("/artifacts", StaticFiles(directory="artifacts"), name="artifacts")


def _valid_signature(body: bytes, header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header)


def _fresh_timestamp(payload: dict, max_skew_seconds: int = 60) -> bool:
    ts = payload.get("webhookTimestamp")
    if ts is None:
        return True
    try:
        ts_ms = int(ts)
    except (TypeError, ValueError):
        return True
    now_ms = int(time.time() * 1000)
    return abs(now_ms - ts_ms) <= max_skew_seconds * 1000


def _seen_recent(issue_id: str) -> bool:
    now = time.time()
    expired = [key for key, ts in _recent_jobs.items() if now - ts > _RECENT_TTL_SECONDS]
    for key in expired:
        _recent_jobs.pop(key, None)
    last_seen = _recent_jobs.get(issue_id)
    if last_seen and (now - last_seen) < _RECENT_TTL_SECONDS:
        return True
    _recent_jobs[issue_id] = now
    return False


# ── Models ────────────────────────────────────────────────────────────────────

class ManualResearchRequest(BaseModel):
    title: str
    description: str = ""
    issue_id: str = "manual-001"
    researchMode: str = "extensive"


class AgentStreamRequest(BaseModel):
    message: str
    conversation_id: str = "hemutchat"
    user_email: str = ""
    history: list = []
    document_context: str = ""
    researchMode: str = "extensive"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "research-agent"}


@app.post("/webhooks/linear")
async def linear_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    sig = request.headers.get("Linear-Signature") or request.headers.get(
        "X-Linear-Signature", ""
    )

    if not _valid_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    if not _fresh_timestamp(payload):
        raise HTTPException(status_code=401, detail="Stale webhook timestamp")

    if payload.get("type") != "Issue" or payload.get("action") not in ("create", "update"):
        return Response(status_code=200)

    issue = payload.get("data", {})
    issue_id = issue.get("id")

    if not issue_id or issue_id in _active_jobs:
        return Response(status_code=200)

    labels = await get_issue_labels(issue_id)
    if RESEARCH_LABEL not in labels:
        return Response(status_code=200)
    if _seen_recent(issue_id):
        log.info("Skipping duplicate webhook  issue=%s", issue_id)
        return Response(status_code=200)

    log.info("Queuing research job  issue=%s  title=%r", issue_id, issue.get("title"))
    _active_jobs.add(issue_id)
    background_tasks.add_task(_run_and_release, issue_id, issue)

    return Response(status_code=200)


@app.post("/research")
async def manual_research(body: ManualResearchRequest):
    result = await run_research_pipeline(
        issue_id=body.issue_id,
        title=body.title,
        description=body.description,
        post_to_linear=False,
        research_mode=body.researchMode,
    )
    pdf_url = f"{PUBLIC_BASE_URL}/artifacts/{result.pdf_path.name}" if result.pdf_path else None
    return {"issue_id": body.issue_id, "digest": result.digest, "pdf_url": pdf_url}


@app.post("/agent/stream")
async def agent_stream(body: AgentStreamRequest):
    async def generate():
        status_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_status(msg: str):
            await status_queue.put(msg)

        async def run_pipeline() -> PipelineResult:
            try:
                result = await run_research_pipeline(
                    issue_id=body.conversation_id,
                    title=body.message,
                    description="",
                    post_to_linear=False,
                    research_mode=body.researchMode,
                    on_status=on_status,
                )
                await status_queue.put(None)  # signal done
                return result
            except Exception as e:
                await status_queue.put(None)
                raise e

        task = asyncio.create_task(run_pipeline())

        # Yield status events as they arrive
        while True:
            msg = await status_queue.get()
            if msg is None:
                break
            status_msg = json.dumps({"type": "status", "status": msg})
            yield f"data: {status_msg}\n\n"

        # Pipeline finished — get result or error
        try:
            result = await task

            result_msg = json.dumps({"type": "text", "content": result.digest})
            yield f"data: {result_msg}\n\n"

            if result.pdf_path is not None:
                pdf_url = f"{PUBLIC_BASE_URL}/artifacts/{result.pdf_path.name}"
                pdf_msg = json.dumps({"type": "pdf", "url": pdf_url})
                yield f"data: {pdf_msg}\n\n"

            done_msg = json.dumps({"type": "done", "messageId": None})
            yield f"data: {done_msg}\n\n"

        except Exception as e:
            log.error("agent_stream error: %s", e)
            error_msg = json.dumps({"type": "error", "error": str(e)})
            yield f"data: {error_msg}\n\n"
            done_msg = json.dumps({"type": "done", "messageId": None})
            yield f"data: {done_msg}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/agent/stop")
async def agent_stop(body: dict):
    conversation_id = body.get("conversation_id", "")
    _active_jobs.discard(conversation_id)
    return {"success": True, "message": "Agent stopped"}


# ── Background task ───────────────────────────────────────────────────────────

async def _run_and_release(issue_id: str, issue: dict):
    try:
        await run_research_pipeline(
            issue_id=issue_id,
            title=issue.get("title", ""),
            description=issue.get("description", ""),
            post_to_linear=True,
        )
    finally:
        _active_jobs.discard(issue_id)
