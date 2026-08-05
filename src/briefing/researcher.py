"""Researcher phase: parse pasted requests from research_requests.md and
research each one via the MCP tools, routed by input type:

  - YouTube URL  -> transcribe_video
  - other URL    -> visit_page
  - bare topic   -> search_feeds first, then google_search/google_news
                     (best-effort — Google tools are bot-blocked from some
                     networks per docs/poc-noapi-google-search-mcp.md, so
                     failures here are caught and noted, never raised)

Findings are returned as a single text block the generator prepends as a
"Requested Research" section. Completed request lines are flipped to
`- [x]` with today's date.
"""

import asyncio
import ipaddress
import re
import socket
import urllib.parse
from datetime import datetime

from . import config, mcp_client

CHECKBOX_RE = re.compile(r"^- \[( |x)\] (.+)$")
YOUTUBE_RE = re.compile(r"(youtube\.com/watch|youtu\.be/)")
URL_RE = re.compile(r"https?://\S+")

# A bare "topic" line gets fed verbatim to search_feeds/google_search as a
# literal query — appropriate for a short phrase ("ai agent orchestrator,
# best practice"), useless (and silently wasteful) for multi-paragraph
# prose that has no line breaks (e.g. an AI-brainstormed research brief
# pasted into one textarea line). See src/panel/app.py's research_run,
# which rejects/redirects text over this length to /research/paste instead
# of queueing it as a topic search.
TOPIC_LENGTH_GUARD_CHARS = 300


def _public_url_error(url: str) -> str | None:
    """SSRF guard for URLs we fetch server-side (visit_page/transcribe run a
    real headless browser). Requests come from a git-committed file today,
    but the control panel plan exposes them via a live web form — a pasted
    metadata/loopback/LAN address must not be fetched and folded into the
    newsletter. Returns a reason string if the URL must be refused."""
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return "no hostname"
    try:
        addrinfos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"could not resolve host: {e}"
    for info in addrinfos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return f"resolves to non-public address {ip}"
    return None


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def parse_requests(text: str) -> list[dict]:
    """Parse `- [ ] ...` / `- [x] ...` lines. Returns list of
    {'line_index', 'checked', 'text'}."""
    requests = []
    for i, line in enumerate(text.splitlines()):
        m = CHECKBOX_RE.match(line)
        if m:
            requests.append({
                "line_index": i,
                "checked": m.group(1) == "x",
                "text": m.group(2).strip(),
            })
    return requests


def classify(request_text: str) -> str:
    if YOUTUBE_RE.search(request_text):
        return "youtube"
    if URL_RE.search(request_text):
        return "url"
    return "topic"


async def research_one(session, request_text: str, phase_cb=None) -> str:
    """Research a single request. Returns a findings string (never raises —
    tool-level failures are caught and turned into a noted skip)."""
    kind = classify(request_text)

    if kind == "youtube":
        if phase_cb:
            phase_cb("transcribing video…")
        url = URL_RE.search(request_text).group(0)
        refusal = _public_url_error(url)
        if refusal:
            return f"### {request_text}\n\n(refused: {refusal})"
        try:
            result = await session.call_tool("transcribe_video", {"url": url})
            return f"### {request_text}\n\n{mcp_client.tool_text(result)}"
        except Exception as e:
            return f"### {request_text}\n\n(transcription failed: {e})"

    if kind == "url":
        if phase_cb:
            phase_cb("fetching page…")
        url = URL_RE.search(request_text).group(0)
        refusal = _public_url_error(url)
        if refusal:
            return f"### {request_text}\n\n(refused: {refusal})"
        try:
            result = await session.call_tool("visit_page", {"url": url})
            return f"### {request_text}\n\n{mcp_client.tool_text(result)}"
        except Exception as e:
            return f"### {request_text}\n\n(page fetch failed: {e})"

    # bare topic — search_feeds first (reliable, local), then best-effort google
    if phase_cb:
        phase_cb("searching stored feeds…")
    findings = []
    try:
        result = await session.call_tool("search_feeds", {"query": request_text, "limit": 5})
        findings.append(mcp_client.tool_text(result))
    except Exception as e:
        findings.append(f"(feed search failed: {e})")

    if phase_cb:
        phase_cb("searching the web (best-effort)…")
    try:
        result = await session.call_tool("google_search", {"query": request_text, "num_results": 5})
        text = mcp_client.tool_text(result)
        if "blocked" in text.lower() or "unusual traffic" in text.lower() or "captcha" in text.lower():
            findings.append("(web search unavailable: blocked by bot detection from this network)")
        else:
            findings.append(text)
    except Exception as e:
        findings.append(f"(web search failed: {e})")

    return f"### {request_text}\n\n" + "\n\n".join(findings)


async def run_pending_async(phase_cb=None, on_result=None) -> tuple[str, int]:
    """Research every unchecked request in research_requests.md. Returns
    (combined_findings, count_processed). Flips processed lines to [x] with
    today's date and writes the file (caller is responsible for the
    pathspec-restricted git commit — see control-panel plan).

    `on_result(request_text, finding_text)`, if given, fires once per
    request immediately after that request's finding is produced — the
    panel uses this to persist each result durably (research_store.py) as
    it completes, rather than only after the whole batch finishes. Optional
    and unused by run.py's scheduled path, which stays on the plain
    combined-string return."""
    if not __import__("os").path.exists(config.RESEARCH_REQUESTS_PATH):
        return "", 0

    with open(config.RESEARCH_REQUESTS_PATH, encoding="utf-8") as f:
        text = f.read()

    requests = [r for r in parse_requests(text) if not r["checked"]]
    if not requests:
        return "", 0

    findings_blocks = []
    processed_texts = []
    today = datetime.now().strftime("%Y-%m-%d")

    async with mcp_client.McpSession() as session:
        for req in requests:
            log(f"Researching: {req['text'][:80]}")
            finding = await research_one(session, req["text"], phase_cb=phase_cb)
            findings_blocks.append(finding)
            processed_texts.append(req["text"])
            if on_result is not None:
                on_result(req["text"], finding)

    # Re-read and patch by CONTENT, not the start-of-run snapshot: research
    # takes minutes, and the panel may have appended new requests meanwhile —
    # writing back the stale snapshot silently erased them (hunt-data #4).
    with open(config.RESEARCH_REQUESTS_PATH, encoding="utf-8") as f:
        current_lines = f.read().splitlines()
    processed = set(processed_texts)
    for i, line in enumerate(current_lines):
        m = CHECKBOX_RE.match(line)
        if m and m.group(1) == " " and m.group(2).strip() in processed:
            current_lines[i] = f"- [x] {m.group(2).strip()} (researched {today})"
    with open(config.RESEARCH_REQUESTS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(current_lines) + "\n")

    return "\n\n".join(findings_blocks), len(requests)


def run_pending() -> tuple[str, int]:
    """Sync entrypoint for the CLI, with the retry-tolerant lock (same
    posture as collector.py — a daily run should wait briefly rather than
    skip research outright if the dashboard is mid-job)."""
    with mcp_client.mcp_lock(retry_seconds=120):
        return asyncio.run(run_pending_async())


async def deep_fetch_items(session, items: list[dict], max_items: int | None = None, phase_cb=None) -> list[dict]:
    """Fetch full source pages for feed items, for social-post material with
    more substance than the digest's one-line summaries. Items with no `url`,
    or whose fetch fails, are skipped entirely rather than falling back to
    the thin stored content — a fallback would just re-condense the same
    shallow text the digest already produced."""
    candidates = [item for item in items if item.get("url")]
    if max_items is not None:
        candidates = candidates[:max_items]

    fetched = []
    for item in candidates:
        url = item["url"]
        refusal = _public_url_error(url)
        if refusal:
            log(f"Skipping {url}: {refusal}")
            continue
        if phase_cb:
            phase_cb(f"fetching {item.get('title', url)[:60]}…")
        try:
            result = await session.call_tool("visit_page", {"url": url})
            content = mcp_client.tool_text(result)
        except Exception as e:
            log(f"Skipping {url}: fetch failed ({e})")
            continue
        fetched.append({"title": item.get("title", ""), "url": url, "content": content})
    return fetched


def deep_fetch_items_sync(items: list[dict], max_items: int | None = None) -> list[dict]:
    """Sync entrypoint mirroring run_pending()'s lock posture, for callers
    (generator.py, the CLI) that aren't already inside an McpSession."""
    async def _run():
        async with mcp_client.McpSession() as session:
            return await deep_fetch_items(session, items, max_items=max_items)

    with mcp_client.mcp_lock(retry_seconds=120):
        return asyncio.run(_run())


if __name__ == "__main__":
    findings, count = run_pending()
    log(f"Processed {count} request(s)")
    if findings:
        print(findings[:2000])
