"""FastAPI control panel for the briefing pipeline.

Localhost-only by contract (panel.sh binds 127.0.0.1) — the /settings page
edits .env credentials, so this must never be exposed on a LAN. See
.omc/plans/2026-07-22-control-panel.md "Network exposure".

Import direction invariant: src/panel imports src/briefing, never the
reverse — the CLI must keep working in a venv without FastAPI installed.
"""

import os
import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# src/ on the path so `from briefing import ...` works however uvicorn is cwd'd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing import mcp_client  # noqa: E402

from . import jobs  # noqa: E402

PANEL_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="AI Briefing Control Panel")
app.mount("/static", StaticFiles(directory=os.path.join(PANEL_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(PANEL_DIR, "templates"))


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse("/preview", status_code=302)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request):
    return templates.TemplateResponse(request, "preview.html", {"active": "preview"})


@app.get("/status", response_class=HTMLResponse)
async def status():
    """Header status dot, polled by htmx every 3s.

    green = idle · amber = a dashboard job is running · red = data/.mcp.lock
    is held (a cron collect/research or another process is mid-run).
    is_locked() is a quick flock probe, cheap enough to poll.
    """
    if mcp_client.is_locked():
        state, label = "lock", "cron/collect running"
    elif jobs.any_running():
        state, label = "busy", "job running"
    else:
        state, label = "idle", "idle"
    return HTMLResponse(
        f'<span id="status-dot" class="dot dot-{state}" title="{label}" '
        f'hx-get="/status" hx-trigger="every 3s" hx-swap="outerHTML">● {label}</span>'
    )
