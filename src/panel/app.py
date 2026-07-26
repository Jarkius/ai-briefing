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
    dashboard-source runs row. Runs on a worker thread via submit_sync.
    Folds in any findings from a completed dashboard research job (the
    panel's equivalent of run.py's research→generate handoff)."""
    findings = state.pop_research_findings()
    conn = db.connect()
    try:
        run_id = db.insert_run(conn, source="dashboard", started_at=datetime.now().isoformat())
        try:
            result = generator.generate(conn, research_findings=findings)
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
    if gen.get("archive_file"):
        db.record_send_status(gen["archive_file"], result)
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
        # Most common cause: the server restarted while a tab was open —
        # jobs live in this process only, so old polling fragments outlive
        # their jobs. Terminal fragment (no hx-trigger) stops the polling.
        return (
            '<div class="banner banner-warn">job no longer exists — the server was '
            'restarted (jobs don\'t survive restarts). Refresh the page.</div>'
        )
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
    if job.name == "research" and isinstance(job.result, str):
        findings = html_lib.escape(job.result)
        return (
            '<div class="banner banner-ok">✓ research done — findings below will be '
            'folded into the next <a href="/preview">Regenerate</a> as a '
            '"Requested Research" section.</div>'
            f'<pre class="findings">{findings}</pre>'
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
        # Stash for the next dashboard Regenerate → "Requested Research"
        # section in the newsletter, mirroring run.py's phase handoff.
        state.set_research_findings(findings)
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
    # Map each completed request to the archive(s) whose research receipt
    # names it — answers "where did my research end up?" with a link.
    request_archives: dict[str, list[dict]] = {}
    for entry in _list_archives():
        for label in entry["research"]:
            request_archives.setdefault(label, []).append(entry)
    for r in reqs:
        # request text minus the "(researched YYYY-MM-DD)" suffix the flip adds
        bare = _re.sub(r"\s*\(researched \d{4}-\d{2}-\d{2}\)$", "", r["text"])
        r["archives"] = request_archives.get(bare, [])
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


@app.post("/research/paste", response_class=HTMLResponse)
async def research_paste(title: str = Form(""), content: str = Form(...)):
    """User already has material (a YouTube summary, an article excerpt,
    notes) — no fetching needed. It joins the research findings verbatim;
    the next Regenerate hands it to the model, which rewrites it into the
    newsletter's own style/sections like any other research."""
    content = content.strip()
    if not content:
        return HTMLResponse(_banner("err", "nothing pasted"))
    title = title.strip() or "pasted material"
    block = (
        f"### {title} (provided by the editor — rewrite into newsletter style, "
        f"do not quote verbatim)\n\n{content}"
    )
    state.add_research_findings(block)
    return HTMLResponse(_banner(
        "ok",
        f"'{title}' filed ({len(content)} chars) — it will be rewritten into the next "
        "Regenerate as part of Requested Research.",
    ))


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
    "PROVIDER_ORDER",
    "BEDROCK_MODEL", "BEDROCK_REGION",
    "GEMINI_API_KEY", "GEMINI_MODEL", "MAXPLUS_API_KEY", "MAXPLUS_MODEL",
    "CLAUDE_CLI_MODEL",
]

KNOWN_PROVIDERS = ["bedrock", "gemini", "maxplus", "claude-cli"]

PROVIDER_LABELS = {
    "bedrock": "Claude — AWS Bedrock (uses this machine's AWS credentials)",
    "gemini": "Google Gemini (direct API, free-tier quota)",
    "maxplus": "maxplus pool (OpenAI-compatible gateway, needs credit)",
    "claude-cli": "Claude CLI (local `claude -p`, uses your subscription)",
}


def _provider_status(name: str) -> str:
    """Availability badge for the settings page — mirrors the generator's
    own _provider_available logic without importing its call table."""
    if name == "bedrock":
        return "enabled" if config.BEDROCK_ENABLED else "disabled"
    if name == "gemini":
        return "key set" if config.GEMINI_API_KEY else "no key"
    if name == "maxplus":
        return "key set" if config.MAXPLUS_API_KEY else "no key"
    if name == "claude-cli":
        import shutil as _shutil
        if not config.CLAUDE_CLI_ENABLED:
            return "disabled"
        return "installed" if _shutil.which("claude") else "not on PATH"
    return "?"


def _read_env_lines() -> list[str]:
    if not os.path.exists(config.ENV_PATH):
        return []
    with open(config.ENV_PATH) as f:
        return f.read().splitlines()


CLAUDE_CLI_MODELS = ["sonnet", "opus", "haiku"]


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    # Effective values: .env/environ where set, else the config default —
    # so BEDROCK_MODEL/REGION etc. never render as misleading empty fields.
    def effective(key: str) -> str:
        env_val = os.environ.get(key, "")
        if env_val:
            return env_val
        return str(getattr(config, key, "") or "")

    values = {k: effective(k) for k in SETTINGS_KEYS if k != "PROVIDER_ORDER"}
    # Current order first, then any known provider not yet in the list
    order = [p for p in config.PROVIDER_ORDER if p in KNOWN_PROVIDERS]
    order += [p for p in KNOWN_PROVIDERS if p not in order]
    providers = [
        {"name": p, "label": PROVIDER_LABELS[p], "status": _provider_status(p)}
        for p in order
    ]
    return templates.TemplateResponse(
        request, "settings.html",
        {"active": "settings", "values": values, "providers": providers,
         "cli_models": CLAUDE_CLI_MODELS},
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    form = await request.form()
    form = dict(form)
    # provider_order arrives as an ordered list of hidden inputs (one per
    # row, in display order) — join into the env value.
    order = [
        v for k, v in sorted(
            ((k, v) for k, v in form.items() if k.startswith("provider_order_")),
            key=lambda kv: int(kv[0].rsplit("_", 1)[1]),
        )
        if v in KNOWN_PROVIDERS
    ]
    if order:
        form["PROVIDER_ORDER"] = ",".join(order)
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


# ---- archive -------------------------------------------------------------------

import re as _re

# briefing_2026-07-25_2028.md or briefing_2026-07-25_232539.md — the full
# document only; the _part1/_part2 siblings are derivable via split_two_parts.
_ARCHIVE_RE = _re.compile(r"^briefing_(\d{4}-\d{2}-\d{2})_(\d{4}|\d{6})\.md$")


_RECEIPT_MARKER = "Requested Research (included in this issue)"


def _archive_research_labels(path: str) -> list[str]:
    """Parse the deterministic research-receipt section from an archive's
    tail (generator appends it last). Returns [] for archives without one."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            tail = f.read().decode(errors="replace")
    except OSError:
        return []
    if _RECEIPT_MARKER not in tail:
        return []
    lines = tail[tail.index(_RECEIPT_MARKER):].splitlines()
    return [ln[2:].strip() for ln in lines if ln.startswith("- ")]


def _list_archives() -> list[dict]:
    """Newest-first list of full-briefing archives with display labels."""
    send_log = db.load_send_status()
    entries = []
    if os.path.isdir(config.ARCHIVE_DIR):
        for fname in os.listdir(config.ARCHIVE_DIR):
            m = _ARCHIVE_RE.match(fname)
            if not m:
                continue
            date_part, time_part = m.groups()
            hh, mm = time_part[:2], time_part[2:4]
            labels = _archive_research_labels(os.path.join(config.ARCHIVE_DIR, fname))
            sent = send_log.get(fname, {})
            entries.append({
                "file": fname, "date": date_part, "time": f"{hh}:{mm}",
                "research": labels,
                # 'sent' | 'partial' | 'error' | '' (never sent / pre-tracking)
                "send_status": sent.get("status", ""),
                "sent_at": (sent.get("at") or "")[:16].replace("T", " "),
            })
    entries.sort(key=lambda e: e["file"], reverse=True)
    return entries


def _archive_date_str(date_part: str) -> str:
    """Human date for the masthead, from the ARCHIVE's date — not today's.
    Same %-d-free construction as generator.generate (Windows-safe)."""
    d = datetime.strptime(date_part, "%Y-%m-%d")
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def _render_archive(fname: str, date_part: str) -> dict:
    """Re-render an archived markdown into the exact two-part HTML the
    sender uses, with the ARCHIVE's own date."""
    with open(os.path.join(config.ARCHIVE_DIR, fname)) as f:
        markdown = f.read()
    date_str = _archive_date_str(date_part)
    p1_md, p2_md = sender.split_two_parts(markdown)
    return {
        "part1": sender.markdown_to_html(p1_md, date_str, title="Daily AI Briefing — Part 1"),
        "part2": sender.markdown_to_html(p2_md, date_str, title="Daily AI Briefing — Part 2"),
        "date_str": date_str,
    }


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request, view: str = ""):
    entries = _list_archives()
    selected = None
    parts = None
    if view:
        # basename() strips any path tricks; then the entry must match a real
        # listed archive — /archive can never read outside ARCHIVE_DIR.
        view = os.path.basename(view)
        selected = next((e for e in entries if e["file"] == view), None)
    if selected is None and entries:
        selected = entries[0]
    if selected:
        parts = _render_archive(selected["file"], selected["date"])
    return templates.TemplateResponse(
        request, "archive.html",
        {"active": "archive", "entries": entries, "selected": selected, "parts": parts},
    )


def _archive_send_job(fname: str, date_part: str) -> dict:
    """Blocking: send an ARCHIVED issue. The dedup pre-check still applies
    (subject carries the archive's date), so re-sending an already-delivered
    edition skips rather than duplicating."""
    parts = _render_archive(fname, date_part)
    conn = db.connect()
    try:
        run_id = db.insert_run(conn, source="dashboard", started_at=datetime.now().isoformat())
        result = sender.send_two_part_briefing(parts["part1"], parts["part2"], parts["date_str"])
        ok = all(not str(v).startswith("error") for v in result.values())
        db.update_run(
            conn, run_id,
            send_status="ok" if ok else "error",
            error_text="" if ok else str(result)[:500],
        )
    finally:
        conn.close()
    db.record_send_status(fname, result)
    return result


@app.post("/archive/send", response_class=HTMLResponse)
async def archive_send(view: str = Form(...)):
    view = os.path.basename(view)
    entry = next((e for e in _list_archives() if e["file"] == view), None)
    if entry is None:
        return HTMLResponse(_banner("err", "unknown archive"))
    job_id = jobs.submit_sync("send", _archive_send_job, entry["file"], entry["date"])
    return HTMLResponse(_job_fragment(job_id))


# ---- logs ----------------------------------------------------------------------

PHASES = ["collect_status", "research_status", "generate_status", "send_status"]


def _phase_strip() -> list[dict]:
    """Latest status per phase across all runs rows (insert-only design:
    a dashboard-only send must not mask a cron collect, so each phase is
    read independently)."""
    strip = []
    try:
        conn = db.connect()
        try:
            for col in PHASES:
                row = db.latest_phase_status(conn, col)
                strip.append({
                    "phase": col.removesuffix("_status"),
                    "status": row[col] if row else "—",
                    "when": (row["started_at"][:16].replace("T", " ") if row else ""),
                    "source": row["source"] if row else "",
                })
        finally:
            conn.close()
    except Exception:
        strip = [{"phase": c.removesuffix("_status"), "status": "—", "when": "", "source": ""}
                 for c in PHASES]
    return strip


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {"active": "logs"})


_LOG_ERR_RE = _re.compile(r"error|failed|failure|traceback|exception|refused|reset", _re.I)
_LOG_OK_RE = _re.compile(r"sent via|Send result.*'sent'|=== Done ===|PASS", _re.I)
_LOG_WARN_RE = _re.compile(r"retry|falling back|skipp|soft.fail|429|quota", _re.I)


def _highlight_log(raw: str) -> str:
    """Escape then classify each line — errors red, wins green, retries
    amber — so a failed 5am run is visible at a glance in the tail."""
    out = []
    for line in raw.splitlines():
        esc = html_lib.escape(line)
        if _LOG_ERR_RE.search(line):
            out.append(f'<span class="ll-err">{esc}</span>')
        elif _LOG_OK_RE.search(line):
            out.append(f'<span class="ll-ok">{esc}</span>')
        elif _LOG_WARN_RE.search(line):
            out.append(f'<span class="ll-warn">{esc}</span>')
        else:
            out.append(esc)
    return "\n".join(out)


@app.get("/logs/tail", response_class=HTMLResponse)
async def logs_tail():
    """Cron pane: briefing.log is written by launchd's stdout redirect —
    dashboard jobs never appear here (they report via the jobs pane below).
    """
    tail = ""
    if os.path.exists(config.LOG_PATH):
        with open(config.LOG_PATH, errors="replace") as f:
            tail = "".join(f.readlines()[-200:])
    jobs_rows = "".join(
        f'<tr><td>{j.started_at}</td><td>{html_lib.escape(j.name)}</td>'
        f'<td class="job-{j.status}">{j.status}</td>'
        f'<td>{html_lib.escape(j.phase_text[:120])}</td></tr>'
        for j in list(jobs.JOBS.values())[-20:]
    ) or '<tr><td colspan="4" class="muted">no dashboard jobs this session</td></tr>'
    strip = "".join(
        f'<span class="phase phase-{s["status"] if s["status"] in ("ok", "error", "soft_fail") else "none"}">'
        f'{s["phase"]}: {s["status"]}'
        f'{" · " + s["when"] + " (" + s["source"] + ")" if s["when"] else ""}</span>'
        for s in _phase_strip()
    )
    return HTMLResponse(
        f'<div id="logs-live" hx-get="/logs/tail" hx-trigger="every 3s" hx-swap="outerHTML">'
        f'<div class="phase-strip">{strip}</div>'
        f'<h2 class="muted">Dashboard jobs (this session)</h2>'
        f'<table class="sources-table"><thead><tr><th>started</th><th>job</th><th>status</th><th>phase</th></tr></thead>'
        f'<tbody>{jobs_rows}</tbody></table>'
        f'<h2 class="muted">briefing.log (cron) — last 200 lines</h2>'
        f'<pre class="logtail">{_highlight_log(tail) or "(no log file yet)"}</pre>'
        f'</div>'
    )


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
