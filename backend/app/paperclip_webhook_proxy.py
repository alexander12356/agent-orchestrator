import json
import logging
import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTask


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
logger = logging.getLogger("uvicorn.error")
MAX_PAYLOAD_SIZE = 25 * 1024 * 1024
PAPERCLIP_URL = "http://127.0.0.1:3101"
ORCHESTRATOR_URL = "http://127.0.0.1:8000/api/webhooks/git/paperclip"


def is_merge(body: bytes, headers: dict[str, str]) -> bool:
    if headers.get("x-github-event") != "pull_request" or not headers.get("x-hub-signature-256"):
        return False
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return False
    if not isinstance(payload, dict) or payload.get("action") != "closed":
        return False
    pr = payload.get("pull_request")
    if not isinstance(pr, dict) or pr.get("merged") is not True:
        return False
    base = pr.get("base")
    return isinstance(base, dict) and base.get("ref") == "main"


async def notify_orchestrator(body: bytes, headers: dict[str, str]) -> None:
    delivery = headers.get("x-github-delivery", "unknown")
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(ORCHESTRATOR_URL, content=body, headers=headers)
        if not response.is_success:
            logger.warning("GitHub delivery %r: orchestrator returned HTTP %d", delivery, response.status_code)
        else:
            logger.info("GitHub delivery %r: orchestrator response %s", delivery, response.text)
    except httpx.RequestError as exc:
        logger.warning("GitHub delivery %r: orchestrator unreachable (%s)", delivery, type(exc).__name__)


@app.post("/api/routine-triggers/public/{public_id}/fire")
async def forward_webhook(public_id: str, request: Request) -> Response:
    if not re.fullmatch(r"[a-f0-9]{24}", public_id) or public_id != os.environ.get("PAPERCLIP_TRIGGER_ID"):
        raise HTTPException(404)

    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_PAYLOAD_SIZE:
            raise HTTPException(413)
        payload.extend(chunk)
    body = bytes(payload)
    headers = {
        name: value for name, value in request.headers.items()
        if name.startswith(("x-github-", "x-hub-"))
        or name in {"accept", "authorization", "content-encoding", "content-type", "user-agent"}
    }
    target = httpx.URL(PAPERCLIP_URL, path=request.url.path, query=request.scope["query_string"] or None)
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.post(target, content=body, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("Paperclip webhook upstream unreachable (%s)", type(exc).__name__)
        raise HTTPException(502, "Paperclip webhook upstream unreachable") from exc

    background = None
    if response.is_success and is_merge(body, headers):
        # Paperclip owns the GitHub response. The orchestrator verifies the same signature with its own settings.
        background = BackgroundTask(notify_orchestrator, body, {name: value for name, value in headers.items() if name != "authorization"})
    return Response(
        response.content,
        status_code=response.status_code,
        headers={name: value for name, value in response.headers.items()
                 if name in {"content-type", "location", "retry-after", "www-authenticate"}},
        background=background,
    )
