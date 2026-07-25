"""FastAPI control panel for the briefing pipeline.

Localhost-only by contract (panel.sh binds 127.0.0.1) — the /settings page
edits .env credentials, so this must never be exposed on a LAN. See
.omc/plans/2026-07-22-control-panel.md "Network exposure".

Import direction invariant: src/panel imports src/briefing, never the
reverse — the CLI must keep working in a venv without FastAPI installed.
"""

import html as html_lib
import os
import subprocess
import sys
from datetime import datetime

from fastapi import Form

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# src/ on the path so `from briefing import ...` works however uvicorn is cwd'd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from briefing import config, db, generator, mcp_client, researcher, sender  # noqa: E402

from . import jobs, state  # noqa: E402

PANEL_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="AI Briefing Control Panel")
app.mount("/static", StaticFiles(directory=os.path.join(PANEL_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(PANEL_DIR, "templates"))


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse("/preview", status_code=302)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request):
    gen = state.get_generation()
    return templates.TemplateResponse(
        request, "preview.html", {"active": "preview", "gen": gen}
    )


def _regenerate_job() -> dict:
    """Blocking: run generate() against current DB state, tracked in a
    dashboard-source runs row. Runs on a worker thread via submit_sync."""
    conn = db.connect()
    try:
        run_id = db.insert_run(conn, source="dashboard", started_at=datetime.now().isoformat())
        try:
            result = generator.generate(conn, research_findings="")
            db.update_run(conn, run_id, generate_status="ok")
        except Exception as e:
            db.update_run(conn, run_id, generate_status="error", error_text=str(e)[:500])
            raise
    finally:
        conn.close()
    state.set_generation(result)
    return result


def _send_job() -> dict:
    """Blocking: send the last generation, tracked in a dashboard runs row."""
    gen = state.get_generation()
    if gen is None:
        raise RuntimeError("nothing generated yet — regenerate first")
    conn = db.connect()
    try:
        run_id = db.insert_run(conn, source="dashboard", started_at=datetime.now().isoformat())
        result = sender.send_two_part_briefing(
            gen["part1_html"], gen["part2_html"], gen["date_str"]
        )
        ok = all(not str(v).startswith("error") for v in result.values())
        db.update_run(
            conn, run_id,
            send_status="ok" if ok else "error",
            error_text="" if ok else str(result)[:500],
        )
    finally:
        conn.close()
    return result


@app.post("/preview/regenerate", response_class=HTMLResponse)
async def preview_regenerate():
    job_id = jobs.submit_sync("regenerate", _regenerate_job)
    return HTMLResponse(_job_fragment(job_id))


@app.post("/preview/send", response_class=HTMLResponse)
async def preview_send():
    if state.get_generation() is None:
        return HTMLResponse('<div class="banner banner-err">Nothing generated yet — regenerate first.</div>')
    job_id = jobs.submit_sync("send", _send_job)
    return HTMLResponse(_job_fragment(job_id))


def _job_fragment(job_id: str) -> str:
    """Polling fragment: keeps hx-trigger while running; terminal renders
    drop the attribute, which is how htmx polling stops."""
    job = jobs.get(job_id)
    if job is None:
        return '<div class="banner banner-err">unknown job</div>'
    phase = html_lib.escape(job.phase_text)
    if job.status == "running":
        return (
            f'<div class="banner banner-live" hx-get="/jobs/{job_id}" '
            f'hx-trigger="every 2s" hx-swap="outerHTML">'
            f'⟳ {html_lib.escape(job.name)}: {phase}</div>'
        )
    if job.status == "error":
        return f'<div class="banner banner-err">✗ {html_lib.escape(job.name)} failed: {phase}</div>'
    # done
    if job.name == "send" and isinstance(job.result, dict):
        parts = ", ".join(f"{k}: {v}" for k, v in job.result.items())
        already = all(v == "already_sent" for v in job.result.values())
        css = "banner-warn" if already else "banner-ok"
        note = "already sent today — nothing re-sent" if already else "sent"
        return f'<div class="banner {css}">✓ {note} ({html_lib.escape(parts)})</div>'
    if job.name == "regenerate":
        return (
            '<div class="banner banner-ok" hx-get="/preview" hx-trigger="load delay:1s" '
            'hx-target="body" hx-swap="innerHTML">✓ regenerated — refreshing preview…</div>'
        )
    return f'<div class="banner banner-ok">✓ {html_lib.escape(job.name)} done</div>'


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_status(job_id: str):
    return HTMLResponse(_job_fragment(job_id))


# ---- research ----------------------------------------------------------------


def _pathspec_commit(message: str, path: str) -> str | None:
    """Pathspec-restricted commit (never bare / add -A — a user's manually
    staged files must never be swept in). Returns an error string for the
    banner, or None on success. 'nothing to commit' is not an error."""
    try:
        subprocess.run(["git", "add", "--", path], cwd=config.REPO_ROOT,
                       capture_output=True, text=True, timeout=15, check=True)
        r = subprocess.run(["git", "commit", "-m", message, "--", path],
                           cwd=config.REPO_ROOT, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return (r.stderr or r.stdout).strip()[:200]
        return None
    except Exception as e:
        return str(e)[:200]


async def _research_job(phase) -> str:
    """Async job: acquire the cross-process lock INSIDE the task (review m5 —
    a sync context manager entered in the route would release at route
    return), then run all pending requests with live phase text."""
    with mcp_client.mcp_lock(retry_seconds=0):
        findings, count = await researcher.run_pending_async(phase_cb=phase)
    if count:
        err = _pathspec_commit(
            "dashboard: research request completed", config.RESEARCH_REQUESTS_PATH
        )
        if err:
            findings += f"\n\n(note: git commit failed: {err})"
    return findings if count else "no unchecked requests found"


@app.get("/research", response_class=HTMLResponse)
async def research(request: Request):
    reqs = []
    if os.path.exists(config.RESEARCH_REQUESTS_PATH):
        with open(config.RESEARCH_REQUESTS_PATH) as f:
            reqs = researcher.parse_requests(f.read())
    reqs.reverse()  # newest first
    # AC7 — a reload reattaches to any live job by re-rendering its polling
    # fragment (in-memory registry is the only place the job exists).
    live = next(
        (jid for jid, j in jobs.JOBS.items()
         if j.name == "research" and j.status == "running"),
        None,
    )
    return templates.TemplateResponse(
        request, "research.html",
        {"active": "research", "requests": reqs,
         "live_fragment": _job_fragment(live) if live else None},
    )


@app.post("/research/run", response_class=HTMLResponse)
async def research_run(text: str = Form("")):
    text = text.strip()
    # Fast pre-reject; the real acquire happens inside the task (TOCTOU gap
    # acceptable single-user — the in-task lock still guarantees exclusion).
    if mcp_client.is_locked():
        return HTMLResponse(
            '<div class="banner banner-warn">Collection is running (lock held) — try again in a minute.</div>'
        )
    if text:
        # Append pasted requests as unchecked lines; the job picks them up.
        lines = [f"- [ ] {ln.strip()}" for ln in text.splitlines() if ln.strip()]
        with open(config.RESEARCH_REQUESTS_PATH, "a") as f:
            f.write("\n" + "\n".join(lines) + "\n")
    job_id = jobs.submit_async("research", _research_job)
    return HTMLResponse(_job_fragment(job_id))


# ---- style -------------------------------------------------------------------


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner banner-{kind}">{html_lib.escape(text)}</div>'


@app.get("/style", response_class=HTMLResponse)
async def style_page(request: Request):
    return templates.TemplateResponse(
        request, "style.html", {"active": "style", "style_text": config.load_style()}
    )


@app.post("/style", response_class=HTMLResponse)
async def style_save(style_text: str = Form("")):
    with open(config.STYLE_PATH, "w") as f:
        f.write(style_text)
    err = _pathspec_commit("dashboard: update newsletter style", config.STYLE_PATH)
    if err:
        return HTMLResponse(_banner("warn", f"saved, but git commit failed: {err}"))
    return HTMLResponse(_banner("ok", "style saved + committed (push stays manual)"))


# ---- sources -------------------------------------------------------------------


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    return templates.TemplateResponse(
        request, "sources.html",
        {"active": "sources", "subs": config.load_subscriptions(),
         "types": sorted(config.KNOWN_SOURCE_TYPES)},
    )


@app.post("/sources", response_class=HTMLResponse)
async def sources_add(
    source_type: str = Form(...), identifier: str = Form(...), name: str = Form(...)
):
    source_type, identifier, name = source_type.strip(), identifier.strip(), name.strip()
    if source_type not in config.KNOWN_SOURCE_TYPES:
        return HTMLResponse(_banner("err", f"unknown source_type '{source_type}'"))
    if not identifier or not name:
        # collector's reconcile key is (type, name) — a nameless entry would
        # be silently skipped forever, so the form requires it up front.
        return HTMLResponse(_banner("err", "identifier and name are both required"))
    subs = config.load_subscriptions()
    if any(s.get("name") == name and s["source_type"] == source_type for s in subs):
        return HTMLResponse(_banner("warn", f"'{name}' already subscribed"))
    subs.append({"source_type": source_type, "identifier": identifier, "name": name})
    config.save_subscriptions(subs)
    err = _pathspec_commit("dashboard: add source", config.SUBSCRIPTIONS_PATH)
    if err:
        return HTMLResponse(_banner("warn", f"saved, but git commit failed: {err}"))
    return HTMLResponse(_banner("ok", f"added '{name}' — subscribes on the next collect run"))


# ---- schedule ------------------------------------------------------------------

PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.user.ai-briefing.plist")


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request):
    import plistlib

    hour, minute, err = 5, 0, None
    if sys.platform == "darwin" and os.path.exists(PLIST_PATH):
        with open(PLIST_PATH, "rb") as f:
            cal = plistlib.load(f).get("StartCalendarInterval", {})
        hour, minute = cal.get("Hour", 5), cal.get("Minute", 0)
    elif sys.platform != "darwin":
        err = "schedule editing is macOS/launchd-only on this machine (Windows uses Task Scheduler)"
    return templates.TemplateResponse(
        request, "schedule.html",
        {"active": "schedule", "hour": hour, "minute": minute, "err": err},
    )


@app.post("/schedule", response_class=HTMLResponse)
async def schedule_save(hour: int = Form(...), minute: int = Form(...)):
    import plistlib

    if sys.platform != "darwin":
        return HTMLResponse(_banner("err", "launchd schedule editing only works on macOS"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return HTMLResponse(_banner("err", "hour must be 0-23, minute 0-59"))
    if not os.path.exists(PLIST_PATH):
        return HTMLResponse(_banner("err", f"plist not found: {PLIST_PATH}"))
    # plistlib round-trip, never string templating (plan step 14).
    with open(PLIST_PATH, "rb") as f:
        plist = plistlib.load(f)
    plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    for action in (["unload"], ["load"]):
        r = subprocess.run(["launchctl", *action, PLIST_PATH],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return HTMLResponse(_banner("err", f"launchctl {action[0]} failed: {r.stderr.strip()[:150]}"))
    return HTMLResponse(_banner("ok", f"schedule set to {hour:02d}:{minute:02d} and reloaded"))


# ---- settings ------------------------------------------------------------------

# Only these keys are exposed/editable — the .env may hold other tooling vars.
SETTINGS_KEYS = [
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL",
    "GEMINI_API_KEY", "GEMINI_MODEL", "MAXPLUS_API_KEY", "MAXPLUS_MODEL",
]


def _read_env_lines() -> list[str]:
    if not os.path.exists(config.ENV_PATH):
        return []
    with open(config.ENV_PATH) as f:
        return f.read().splitlines()


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    values = {k: os.environ.get(k, "") for k in SETTINGS_KEYS}
    return templates.TemplateResponse(
        request, "settings.html", {"active": "settings", "values": values}
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    # Rewrite only known keys in place, preserving unrelated lines/comments.
    # Values are NEVER logged (plan step 15).
    lines = _read_env_lines()
    seen = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in SETTINGS_KEYS and key in form:
                lines[i] = f"{key}={form[key]}"
                seen.add(key)
    for key in SETTINGS_KEYS:
        if key in form and key not in seen and str(form[key]).strip():
            lines.append(f"{key}={form[key]}")
    with open(config.ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(config.ENV_PATH, 0o600)
    # Review M2: without this, the running server keeps stale constants and
    # the next regenerate/send silently uses the old values.
    config.reload()
    return HTMLResponse(_banner("ok", "settings saved — applied to this server immediately (.env stays gitignored)"))


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
