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

from briefing import collector, config, db, generator, gmail_api, mcp_client, research_store, researcher, sender  # noqa: E402

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
    # Pending-work strip: what's waiting to go into (or out of) an edition.
    pending_requests = []
    if os.path.exists(config.RESEARCH_REQUESTS_PATH):
        with open(config.RESEARCH_REQUESTS_PATH, encoding="utf-8") as f:
            pending_requests = [
                r["text"] for r in researcher.parse_requests(f.read()) if not r["checked"]
            ]
    research_conn = research_store.connect()
    try:
        ready = research_store.list_ready(research_conn)
    finally:
        research_conn.close()
    pending = {
        "requests": pending_requests,
        "findings_chars": sum(len(t.get("result_text") or "") for t in ready),
        "drafts": sum(1 for e in _list_archives() if not e["send_status"]),
    }
    return templates.TemplateResponse(
        request, "preview.html", {
            "active": "preview", "gen": gen, "pending": pending,
            "social_post": state.get_social_post(),
            "social_post_sections": list(enumerate(SOCIAL_POST_SECTION_LABELS)),
        }
    )


def _regenerate_job() -> dict:
    """Blocking: run generate() against current DB state, tracked in a
    dashboard-source runs row. Runs on a worker thread via submit_sync.
    Folds in any research_store-'ready' findings (the panel's equivalent
    of run.py's research→generate handoff).

    Consumption is recorded ONLY after generate() succeeds and produces an
    archive file — if this process dies between generate() returning and
    mark_consumed running, the tasks stay 'ready' (visible, re-includable
    on the next Regenerate) rather than silently vanishing or being
    falsely marked included in an edition they never reached. A duplicate
    inclusion on a retried Regenerate is an acceptable cost; silent loss
    is not."""
    research_conn = research_store.connect()
    try:
        ready = research_store.list_ready(research_conn)
        findings = "\n\n".join(t["result_text"] for t in ready if t.get("result_text"))

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

        if ready and result.get("archive_file"):
            research_store.mark_consumed(
                research_conn, [t["id"] for t in ready], result["archive_file"]
            )
    finally:
        research_conn.close()
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
    # Double-click guard: two concurrent regenerates race on consuming the
    # same 'ready' research tasks and last-writer-wins the preview state —
    # reattach to the running job instead of spawning a second.
    existing = jobs.running_job("regenerate")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync("regenerate", _regenerate_job)
    return HTMLResponse(_job_fragment(job_id))


@app.post("/preview/send", response_class=HTMLResponse)
async def preview_send():
    if state.get_generation() is None:
        return HTMLResponse('<div class="banner banner-err">Nothing generated yet — regenerate first.</div>')
    # Double-click guard: the IMAP/API dedup pre-check is check-then-act —
    # two simultaneous sends could both pass it and double-deliver.
    existing = jobs.running_job("send")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync("send", _send_job)
    return HTMLResponse(_job_fragment(job_id))


# Short editor-facing labels for the section checkboxes — SECTION_PROMPTS'
# own labels ("1/6: top stories + news") are dev-facing call-order markers.
SOCIAL_POST_SECTION_LABELS = [
    "Top Stories & News",
    "Governance & Mindset",
    "Learning & Best Practices",
    "Security & Ethics",
    "Research & Tools",
    "Technical Deep-Dive",
]


def _social_post_job(section_indices: list[int]) -> dict:
    """Blocking: deep-fetch source items for the given sections (empty list
    = every section, capped per-section — see
    generator.social_post_candidate_items), then generate one social post.
    Raises if nothing was fetchable, so the job surfaces as a clear error
    banner instead of silently producing a post from thin air."""
    conn = db.connect()
    try:
        candidates = generator.social_post_candidate_items(
            conn, section_indices=section_indices or None,
        )
    finally:
        conn.close()
    fetched = researcher.deep_fetch_items_sync(candidates)
    if not fetched:
        raise RuntimeError(
            "no fetchable sources in this selection (no URLs, or all fetches failed) — "
            "try a different section"
        )
    source_material = generator.build_social_post_source(fetched)
    now = datetime.now()
    # No %-d (glibc/BSD-only, raises on Windows) — same construction as
    # generator.generate()'s date_str.
    date_str = f"{now.strftime('%A, %B')} {now.day}, {now.year}"
    post_text = generator.generate_social_post(source_material, date_str)
    result = {"post_text": post_text, "date_str": date_str}
    state.set_social_post(result)
    return result


def _social_post_send_job() -> str:
    """Blocking: send the last generated social post."""
    post = state.get_social_post()
    if post is None:
        raise RuntimeError("nothing generated yet — build a social post first")
    post_html = sender.render_social_post_html(post["post_text"], post["date_str"])
    return sender.send_social_post_email(post_html, post["date_str"])


@app.post("/preview/social-post", response_class=HTMLResponse)
async def preview_social_post(sections: list[int] = Form([])):
    existing = jobs.running_job("social-post")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync("social-post", _social_post_job, sections)
    return HTMLResponse(_job_fragment(job_id))


@app.post("/preview/social-post/send", response_class=HTMLResponse)
async def preview_social_post_send():
    if state.get_social_post() is None:
        return HTMLResponse('<div class="banner banner-err">Nothing generated yet — build a social post first.</div>')
    existing = jobs.running_job("social-post-send")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync("social-post-send", _social_post_send_job)
    return HTMLResponse(_job_fragment(job_id))


def _archive_badge_oob(fname: str) -> str:
    """Out-of-band fragment patching one archive-list row's badge in place.
    Mirrors the badge markup in archive.html's #for e in entries# loop —
    keep both in sync if that block changes. `just-updated` triggers the
    badge-pop CSS animation (panel.css) so a send from the detail pane is
    visible in the list without a full-page reload."""
    entry = next((e for e in _list_archives() if e["file"] == fname), None)
    if entry is None:
        return ""
    if entry["send_status"] == "sent":
        badge = f'<span class="badge-sent" title="emailed {html_lib.escape(entry["sent_at"])}">✉ sent</span>'
    elif entry["send_status"] == "error":
        badge = f'<span class="badge-senderr" title="send failed {html_lib.escape(entry["sent_at"])}">✉ failed</span>'
    elif entry["send_status"] == "partial":
        badge = f'<span class="badge-senderr" title="partially sent {html_lib.escape(entry["sent_at"])}">✉ partial</span>'
    else:
        badge = (
            '<span class="badge-unsent" title="generated but never emailed — '
            'open it and click \'Send this edition\'">draft</span>'
        )
    if entry["research"]:
        titles = html_lib.escape(", ".join(entry["research"]))
        badge += f'<span class="badge-research" title="research: {titles}">🔍</span>'
    dom_id = f'a-badges-{fname.replace(".", "-")}'
    badge_oob = f'<span id="{dom_id}" class="a-badges just-updated" hx-swap-oob="true">{badge}</span>'
    # The detail-pane button's label ("Send this edition" -> "Send again")
    # is stale after a send too — only relevant while that file is open.
    btn_id = f'a-send-btn-{fname.replace(".", "-")}'
    btn_label = "Send again" if entry["send_status"] == "sent" else "Send this edition"
    btn_oob = (
        f'<button type="submit" class="btn-send" id="{btn_id}" hx-swap-oob="true">'
        f'{btn_label}</button>'
    )
    return badge_oob + btn_oob


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
        banner = f'<div class="banner {css}">✓ {note} ({html_lib.escape(parts)})</div>'
        # Archive-detail sends (job.target set) patch the list badge in
        # place via OOB swap — otherwise the banner (targeted at #banner,
        # inside .archive-view) is the only visible confirmation, and the
        # list row still says "draft" until a full page reload.
        if job.target:
            banner += _archive_badge_oob(job.target)
        return banner
    if job.name == "regenerate":
        return (
            '<div class="banner banner-ok" hx-get="/preview" hx-trigger="load delay:1s" '
            'hx-target="body" hx-swap="innerHTML">✓ regenerated — refreshing preview…</div>'
        )
    if job.name == "social-post":
        return (
            '<div class="banner banner-ok" hx-get="/preview" hx-trigger="load delay:1s" '
            'hx-target="body" hx-swap="innerHTML">✓ social post ready — refreshing preview…</div>'
        )
    if job.name == "social-post-send" and isinstance(job.result, str):
        result = job.result
        if result.startswith("error"):
            return f'<div class="banner banner-err">✗ social post send failed: {html_lib.escape(result)}</div>'
        css = "banner-warn" if result == "already_sent" else "banner-ok"
        note = "already sent today" if result == "already_sent" else "sent"
        return f'<div class="banner {css}">✓ social post {note}</div>'
    if job.name == "research" and isinstance(job.result, str):
        findings = html_lib.escape(job.result)
        return (
            '<div class="banner banner-ok">✓ research done — findings below will be '
            'folded into the next <a href="/preview">Regenerate</a> as a '
            '"Requested Research" section.</div>'
            f'<pre class="findings">{findings}</pre>'
        )
    if job.name == "disable-source" and isinstance(job.result, str):
        name = job.result
        return f'<div class="banner banner-ok">✓ \'{html_lib.escape(name)}\' disabled and archived</div>' + _source_row_oob(name)
    if job.name == "gmail-reauth":
        return (
            '<div class="banner banner-ok" hx-get="/settings" hx-trigger="load delay:1s" '
            'hx-target="body" hx-swap="innerHTML">✓ re-authorized — Gmail send is good for '
            'another 7 days.</div>'
        )
    return f'<div class="banner banner-ok">✓ {html_lib.escape(job.name)} done</div>'


def _source_dom_id(name: str) -> str:
    """Must match sources.html's `id="src-row-..."` filter chain exactly."""
    return name.replace(" ", "-").replace("/", "-").lower()


def _source_row_oob(name: str) -> str:
    """Out-of-band fragment patching one Sources-table row in place after a
    toggle — renders the same _source_row.html partial sources.html's loop
    uses, so the two can never drift."""
    sub = next((s for s in config.load_subscriptions() if s.get("name") == name), None)
    if sub is None:
        return ""
    row_html = templates.get_template("_source_row.html").render(
        s=sub, warn_threshold=config.FAILURE_WARN_THRESHOLD,
        disable_threshold=config.FAILURE_DISABLE_THRESHOLD,
    )
    dom_id = f"src-row-{_source_dom_id(name)}"
    return f'<tr id="{dom_id}" class="just-updated" hx-swap-oob="true">{row_html}</tr>'


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
    return), then run all pending requests with live phase text.

    Each result is persisted to research_store durably AS IT COMPLETES
    (on_result) — this is now the sole source _regenerate_job reads from
    (see below), replacing state.py's LAST_RESEARCH_FINDINGS for this
    handoff. Previously, the research_requests.md checkbox flipped to done
    the moment this function returned, regardless of whether the findings
    survived a restart or ever reached a generated edition — an in-process
    global with no durability. A user's pasted research was found checked
    off with zero trace in any archive: permanently lost with no
    indication anything had gone wrong. The checkbox now means "the
    research function returned"; research_store's 'ready'/'consumed'
    states are what actually track durability and inclusion."""
    conn = research_store.connect()
    try:
        def on_result(request_text, finding_text):
            research_store.mark_ready(conn, research_store.insert_queued(conn, request_text), finding_text)

        with mcp_client.mcp_lock(retry_seconds=0):
            findings, count = await researcher.run_pending_async(phase_cb=phase, on_result=on_result)
    finally:
        conn.close()
    if count:
        err = _pathspec_commit(
            "dashboard: research request completed", config.RESEARCH_REQUESTS_PATH
        )
        if err:
            findings += f"\n\n(note: git commit failed: {err})"
    return findings if count else "no unchecked requests found"


@app.get("/research", response_class=HTMLResponse)
async def research(request: Request, q: str = ""):
    # Still-unprocessed requests come from the checkbox file — a
    # research_store row only exists once research_one() has actually
    # returned a result (see _research_job's on_result).
    pending = []
    if os.path.exists(config.RESEARCH_REQUESTS_PATH):
        with open(config.RESEARCH_REQUESTS_PATH, encoding="utf-8") as f:
            pending = [r for r in researcher.parse_requests(f.read()) if not r["checked"]]
    pending.reverse()  # newest first

    # Completed research — sourced from research_store (durable, has a
    # real id + state + archive_file), not from string-matching the
    # checked line's text against a "### {label}" receipt heading baked
    # into the archive's tail. That matching was fragile by construction
    # (any whitespace/wording drift silently breaks the link) and simply
    # didn't exist for /research/paste entries, which never wrote a
    # receipt at all — this is also where pasted material becomes visible
    # in this list for the first time.
    q = q.strip()
    research_conn = research_store.connect()
    try:
        tasks = research_store.search_tasks(research_conn, q) if q else research_store.list_tasks(research_conn)
    finally:
        research_conn.close()
    for t in tasks:
        t["archive_entry"] = None
        if t.get("archive_file"):
            t["archive_entry"] = next(
                (e for e in _list_archives() if e["file"] == t["archive_file"]), None
            )

    # AC7 — a reload reattaches to any live job by re-rendering its polling
    # fragment (in-memory registry is the only place the job exists).
    live = next(
        (jid for jid, j in jobs.JOBS.items()
         if j.name == "research" and j.status == "running"),
        None,
    )
    return templates.TemplateResponse(
        request, "research.html",
        {"active": "research", "pending": pending, "tasks": tasks, "q": q,
         "live_fragment": _job_fragment(live) if live else None},
    )


@app.get("/research/{task_id}", response_class=HTMLResponse)
async def research_detail(request: Request, task_id: int):
    research_conn = research_store.connect()
    try:
        task = research_store.get_task(research_conn, task_id)
    finally:
        research_conn.close()
    if task is None:
        return HTMLResponse(
            f'<div class="banner banner-warn">No research task #{task_id}.</div>',
            status_code=404,
        )
    archive_entry = None
    if task.get("archive_file"):
        archive_entry = next(
            (e for e in _list_archives() if e["file"] == task["archive_file"]), None
        )
    return templates.TemplateResponse(
        request, "research_detail.html",
        {"active": "research", "task": task, "archive_entry": archive_entry},
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
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # A long non-URL/non-YouTube line has no line breaks of its own —
        # it's prose (an AI-brainstormed brief, notes, an article excerpt),
        # not a topic phrase. Queued as a "topic" it gets fed verbatim to
        # search_feeds/google_search as one literal query, which returns
        # garbage, and the checkbox still flips to done — silently losing
        # real material with no indication anything went wrong. Reject and
        # redirect to /research/paste instead, which files it as findings
        # directly with no lossy search round-trip.
        oversized = [
            ln for ln in lines
            if len(ln) > researcher.TOPIC_LENGTH_GUARD_CHARS
            and not researcher.YOUTUBE_RE.search(ln)
            and not researcher.URL_RE.search(ln)
        ]
        if oversized:
            return HTMLResponse(_banner(
                "warn",
                f"{len(oversized)} line(s) look like pasted material, not a topic/URL "
                f"(over {researcher.TOPIC_LENGTH_GUARD_CHARS} chars, no link) — "
                "use “Already have the material? Paste it directly” below instead. "
                "Nothing was queued.",
            ))
        # Append pasted requests as unchecked lines; the job picks them up.
        with open(config.RESEARCH_REQUESTS_PATH, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(f"- [ ] {ln}" for ln in lines) + "\n")
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
    conn = research_store.connect()
    try:
        task_id = research_store.insert_queued(conn, title)
        research_store.mark_ready(conn, task_id, block)
    finally:
        conn.close()
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
    with open(config.STYLE_PATH, "w", encoding="utf-8") as f:
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
         "types": sorted(config.KNOWN_SOURCE_TYPES),
         "warn_threshold": config.FAILURE_WARN_THRESHOLD,
         "disable_threshold": config.FAILURE_DISABLE_THRESHOLD},
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


async def _disable_source_job(name: str) -> str:
    """Async job: archive + unsubscribe + disable one source. Acquires the
    cross-process lock INSIDE the task (same reasoning as _research_job —
    a sync context manager entered in the route would release at route
    return, before the awaited MCP call actually runs)."""
    conn = db.connect()
    try:
        with mcp_client.mcp_lock(retry_seconds=0):
            async with mcp_client.McpSession() as session:
                await collector._disable_source(session, conn, name)
    finally:
        conn.close()
    return name


@app.post("/sources/toggle", response_class=HTMLResponse)
async def sources_toggle(name: str = Form(...), enable: str = Form(...)):
    name = name.strip()
    if enable == "true":
        # No MCP call needed — re-subscribing happens naturally on the next
        # collect run's _reconcile(), which skips already-subscribed names.
        subs = config.load_subscriptions()
        sub = next((s for s in subs if s.get("name") == name), None)
        if sub is None:
            return HTMLResponse(_banner("err", f"'{name}' not found"))
        sub["enabled"] = True
        sub["consecutive_failures"] = 0
        config.save_subscriptions(subs)
        err = _pathspec_commit(f"dashboard: enable source '{name}'", config.SUBSCRIPTIONS_PATH)
        if err:
            return HTMLResponse(_banner("warn", f"enabled, but git commit failed: {err}"))
        return HTMLResponse(_banner("ok", f"'{name}' enabled — subscribes on the next collect run"))

    # Disabling talks to the MCP server (unsubscribe) — same job/lock
    # pattern as research, not a plain synchronous route.
    if mcp_client.is_locked():
        return HTMLResponse(
            '<div class="banner banner-warn">Collection is running (lock held) — try again in a minute.</div>'
        )
    existing = jobs.running_job("disable-source")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_async("disable-source", lambda phase: _disable_source_job(name))
    return HTMLResponse(_job_fragment(job_id))


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
    with open(config.ENV_PATH, encoding="utf-8") as f:
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
         "cli_models": CLAUDE_CLI_MODELS, "oauth": gmail_api.token_status()},
    )


def _oauth_reauth_job() -> str:
    """Blocking: opens a local browser for the human consent click. Runs on
    a worker thread like every other job — the click itself can't be
    automated, but starting/tracking it from the panel means no terminal."""
    gmail_api.run_oauth_consent()
    return "reauthorized"


@app.post("/settings/gmail-reauth", response_class=HTMLResponse)
async def settings_gmail_reauth():
    existing = jobs.running_job("gmail-reauth")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync("gmail-reauth", _oauth_reauth_job)
    return HTMLResponse(_job_fragment(job_id))


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

    # A key with no active .env line is either genuinely new (write it to
    # .env, same as always) or currently sourced from Bitwarden — for that
    # case, edit Bitwarden itself rather than silently creating a local
    # .env override that would win over Bitwarden from then on (the exact
    # precedence footgun the whole Bitwarden design exists to avoid).
    #
    # bws_list_secrets() returning None is ambiguous on a machine that HAS
    # done Bitwarden setup: it could mean "this key genuinely isn't in
    # Bitwarden" (fine, .env is correct) or "the list call itself failed"
    # (NOT fine — we can't tell which no-active-line keys are Bitwarden-
    # sourced, so silently writing any of them to .env would reintroduce
    # the exact bug this feature fixes). Only trust "not in Bitwarden" when
    # bws_is_available() is False — a machine that never set up Bitwarden
    # at all — or when the list call actually succeeded.
    lines = _read_env_lines()
    active_keys = {
        stripped.partition("=")[0].strip()
        for line in lines
        for stripped in [line.strip()]
        if stripped and not stripped.startswith("#") and "=" in stripped
    }
    bitwarden_configured = config.bws_is_available()
    bitwarden_secrets = config.bws_list_secrets() if bitwarden_configured else None
    bitwarden_list_failed = bitwarden_configured and bitwarden_secrets is None
    bitwarden_keys = {s["key"] for s in bitwarden_secrets} if bitwarden_secrets else set()

    dotenv_form = {}
    bitwarden_written = []
    bitwarden_errors = []
    unroutable = []
    for key, value in form.items():
        if key not in SETTINGS_KEYS:
            continue
        if key in active_keys:
            dotenv_form[key] = value
        elif key in bitwarden_keys:
            if not str(value).strip():
                # Mirrors the .env path's own guard below (skip blanks) —
                # a Bitwarden-sourced field left blank must never wipe the
                # real secret; there's no confirmation step here.
                continue
            ok, err = config.bws_write_secret(key, value, secrets=bitwarden_secrets)
            (bitwarden_written if ok else bitwarden_errors).append(key if ok else f"{key} ({err})")
        elif bitwarden_list_failed:
            unroutable.append(key)
        else:
            dotenv_form[key] = value

    # Rewrite only known keys in place, preserving unrelated lines/comments.
    # Values are NEVER logged (plan step 15).
    seen = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in dotenv_form:
                lines[i] = f"{key}={dotenv_form[key]}"
                seen.add(key)
    for key, value in dotenv_form.items():
        if key not in seen and str(value).strip():
            lines.append(f"{key}={value}")
    with open(config.ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    config.restrict_to_owner_only(config.ENV_PATH)
    # Review M2: without this, the running server keeps stale constants and
    # the next regenerate/send silently uses the old values.
    config.reload()

    if unroutable:
        return HTMLResponse(_banner(
            "err",
            f"Bitwarden is configured but couldn't be reached to verify these keys — "
            f"nothing was changed for: {', '.join(unroutable)}. Try again.",
        ))
    if bitwarden_errors:
        ok_parts = []
        if dotenv_form:
            ok_parts.append(f".env: {', '.join(dotenv_form)}")
        if bitwarden_written:
            ok_parts.append(f"Bitwarden: {', '.join(bitwarden_written)}")
        ok_summary = f" (succeeded — {'; '.join(ok_parts)})" if ok_parts else ""
        return HTMLResponse(_banner(
            "err", f"Bitwarden write failed for: {', '.join(bitwarden_errors)}{ok_summary}",
        ))
    parts = []
    if dotenv_form:
        parts.append(".env (this server only)")
    if bitwarden_written:
        parts.append(f"Bitwarden (all machines: {', '.join(bitwarden_written)})")
    return HTMLResponse(_banner("ok", f"settings saved — {' + '.join(parts)}" if parts else "no changes"))


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
            # Shape-valid but calendar-invalid dates (2026-13-45) would crash
            # rendering when auto-selected as entries[0] — skip them here.
            try:
                datetime.strptime(date_part, "%Y-%m-%d")
            except ValueError:
                continue
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
    # The filename regex checks digit SHAPE only — 2026-13-45 matches it but
    # isn't a calendar date, and one such file must not 500 the whole tab.
    try:
        d = datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        return date_part
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def _render_archive(fname: str, date_part: str) -> dict:
    """Re-render an archived edition with the archive's own date.

    Part 1 and Part 2 are the exact two-part sender contract. A research
    sibling is optional and display-only; old archives simply return None.
    """
    archive_path = os.path.join(config.ARCHIVE_DIR, fname)
    with open(archive_path, encoding="utf-8") as f:
        markdown = f.read()
    date_str = _archive_date_str(date_part)
    p1_md, p2_md = sender.split_two_parts(markdown)
    part3 = None
    part3_path = os.path.join(config.ARCHIVE_DIR, fname[:-3] + "_part3_research.md")
    if os.path.isfile(part3_path):
        with open(part3_path, encoding="utf-8") as f:
            part3 = sender.markdown_to_html(
                f.read(), date_str, title="Daily AI Briefing — Part 3 · Requested Research"
            )
    return {
        "part1": sender.markdown_to_html(p1_md, date_str, title="Daily AI Briefing — Part 1"),
        "part2": sender.markdown_to_html(p2_md, date_str, title="Daily AI Briefing — Part 2"),
        "part3": part3,
        "date_str": date_str,
    }


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request, view: str = "", show: str = "all"):
    entries = _list_archives()
    if show == "sent":
        entries = [e for e in entries if e["send_status"] == "sent"]
    elif show == "drafts":
        entries = [e for e in entries if not e["send_status"]]
    selected = None
    parts = None
    not_found = ""
    if view:
        # basename() strips any path tricks; then the entry must match a real
        # listed archive — /archive can never read outside ARCHIVE_DIR.
        view = os.path.basename(view)
        selected = next((e for e in entries if e["file"] == view), None)
        if selected is None:
            # Explicit not-found instead of silently serving the newest —
            # a stale bookmark or filtered-out entry shouldn't masquerade
            # as a different edition.
            not_found = view
    if selected is None and entries and not not_found:
        selected = entries[0]
    if selected:
        parts = _render_archive(selected["file"], selected["date"])
    return templates.TemplateResponse(
        request, "archive.html",
        {"active": "archive", "entries": entries, "selected": selected,
         "parts": parts, "show": show, "not_found": not_found},
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
    existing = jobs.running_job("send")
    if existing:
        return HTMLResponse(_job_fragment(existing))
    job_id = jobs.submit_sync(
        "send", _archive_send_job, entry["file"], entry["date"], target=entry["file"],
    )
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
        with open(config.LOG_PATH, encoding="utf-8", errors="replace") as f:
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
        f'<div id="logs-live" hx-get="/logs/tail" hx-trigger="every 3s" hx-swap="outerHTML" '
        f'role="status" aria-live="polite">'
        f'<div class="phase-strip">{strip}</div>'
        f'<h2 class="muted">Dashboard jobs (this session)</h2>'
        f'<div class="table-scroll"><table class="sources-table"><thead><tr><th>started</th><th>job</th><th>status</th><th>phase</th></tr></thead>'
        f'<tbody>{jobs_rows}</tbody></table></div>'
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

    Gmail token expiry check rides this same poll (site-wide, every page —
    Settings isn't where you're looking when 5am is what matters) rather
    than adding a second poller; os.path.getmtime is equally cheap.
    """
    if mcp_client.is_locked():
        state, label = "lock", "cron/collect running"
    elif jobs.any_running():
        state, label = "busy", "job running"
    else:
        state, label = "idle", "idle"

    oauth = gmail_api.token_status()
    oauth_html = ""
    if oauth["state"] == "expired":
        oauth_html = ' <a href="/settings" class="dot-warn" title="Gmail token expired — send may be broken">✉⚠</a>'
    elif oauth["state"] == "expiring_soon":
        oauth_html = f' <a href="/settings" class="dot-warn" title="Gmail token expires in {oauth["days_left"]}d">✉{oauth["days_left"]:g}d</a>'

    return HTMLResponse(
        f'<span id="status-dot" class="dot dot-{state}" title="{label}" role="status" aria-live="polite" '
        f'hx-get="/status" hx-trigger="every 3s" hx-swap="outerHTML">'
        f'<span class="dot-label">{label}</span></span>'
        f'{oauth_html}'
    )
