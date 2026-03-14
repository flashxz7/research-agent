from fastapi import FastAPI, Request
import os

app = FastAPI()

@app.get("/")
def root():
    return {"status": "research-agent running"}

@app.post("/webhooks/linear")
async def linear_webhook(request: Request):
    payload = await request.json()

    print("\n--- LINEAR WEBHOOK RECEIVED ---")
    print(payload)

    # extract useful fields
    issue = payload.get("data", {})
    title = issue.get("title")
    description = issue.get("description")

    print("Issue title:", title)
    print("Issue description:", description)

    return {"status": "received"}