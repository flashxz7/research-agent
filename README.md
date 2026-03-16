# Hemut Research Agent

Automated deep-research pipeline that turns a Linear issue or manual request into a cited, auditable report. The system is optimized for Hemut’s trucking/TMS context, but it can handle any research-style question. Every factual claim is expected to be sourced, verified, and delivered in a readable report with clear provenance.

## What This Does

- Accepts a Linear webhook or manual POST request.
- Performs deep, multi-source research using Perplexity.
- Extracts and verifies individual claims against the cited sources.
- Produces a clean, human-readable report with verification notes.
- Posts a digest back to Linear (optional).
- Saves full artifacts for audit and debugging.

## Architecture Overview

Linear Issue or Manual Request
? FastAPI endpoint
? Perplexity Deep Research
? Claim extraction
? Verification (exact, fuzzy, semantic)
? Report formatting
? Artifact storage
? Optional Linear comment

## Repository Layout

- `main.py` FastAPI app, webhook handler, manual endpoint, idempotency.
- `pipeline.py` End-to-end orchestration and artifact storage.
- `prompts.py` Intent classification and dynamic prompt construction.
- `perplexity_client.py` Perplexity API client with retry logic.
- `verification.py` Claim verification engine and scoring.
- `formats.py` Report formatting and verification footer.
- `linear_client.py` Linear GraphQL client with retry logic.
- `artifacts/` Saved inputs, outputs, and verification traces.

## Setup

1. Create a virtual environment and install dependencies.

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2. Create `.env` based on `.env.example`.

Required:
- `PERPLEXITY_API_KEY`

Optional:
- `OPENAI_API_KEY` (used only to format the report; no new facts are added)
- `LINEAR_API_KEY` (to post back to Linear)
- `LINEAR_WEBHOOK_SECRET` (webhook signature validation)

## Run Locally (Manual Test)

```bash
uvicorn main:app --reload --port 8000
```

Then send a manual request:

```bash
$body = @{
  title = "Common problems with McLeod TMS onboarding"
  description = "Focus on causes and mitigation."
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/research" -Method POST -ContentType "application/json" -Body $body
```

The response includes the full formatted digest. All artifacts are stored in `artifacts/`.

## Run with Linear Webhooks

1. Start the server.
2. Expose it publicly (ngrok or equivalent).
3. Create a Linear webhook pointing to:
   - `POST /webhooks/linear`

When an issue is created or updated with the `research-agent` label, the pipeline runs and posts the digest back to the issue.

## Output Guarantees

The prompt system enforces:

- Deep research with named sources and explicit citations.
- Minimum output density and section-level coverage requirements.
- A strict “no recommendations” rule — it reports facts, not opinions.
- A Hemut-specific implication line after each Key Finding when relevant.

## Verification Model

Verification is conservative and transparent:

- Exact match ? fuzzy token overlap ? semantic similarity.
- Each claim is tagged as VERIFIED, PARTIAL, UNVERIFIED, or SOURCE_UNREACHABLE.
- Unverified claims are preserved with caution tags rather than silently dropped.

## Artifacts

Every run saves a JSON file in `artifacts/` containing:

- Raw Perplexity output
- Citations list
- Classification metadata
- Verification map and scores
- Final formatted digest

This makes the pipeline auditable and easy to debug.

## Troubleshooting

- `401 Unauthorized` from Linear: check `LINEAR_API_KEY`.
- `401 Invalid signature`: check `LINEAR_WEBHOOK_SECRET` or disable during local testing.
- `403/429` from sources: some sites block scraping; these are marked as unreachable.
- Very short output: Perplexity did not meet depth requirements; check prompt logs.

## Roadmap

- Diffbot/Playwright canonical extraction
- Supabase storage + embeddings
- RAG reuse of verified claims
- Slack and Hemut Chat integration

## License

Internal use. Add a license if you plan to distribute.
