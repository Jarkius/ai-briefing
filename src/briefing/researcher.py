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
import re
from datetime import datetime

from . import config, mcp_client

CHECKBOX_RE = re.compile(r"^- \[( |x)\] (.+)$")
YOUTUBE_RE = re.compile(r"(youtube\.com/watch|youtu\.be/)")
URL_RE = re.compile(r"https?://\S+")


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
        url_match = URL_RE.search(request_text)
        try:
            result = await session.call_tool("transcribe_video", {"url": url_match.group(0)})
            return f"### {request_text}\n\n{mcp_client.tool_text(result)}"
        except Exception as e:
            return f"### {request_text}\n\n(transcription failed: {e})"

    if kind == "url":
        if phase_cb:
            phase_cb("fetching page…")
        url_match = URL_RE.search(request_text)
        try:
            result = await session.call_tool("visit_page", {"url": url_match.group(0)})
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


async def run_pending_async(phase_cb=None) -> tuple[str, int]:
    """Research every unchecked request in research_requests.md. Returns
    (combined_findings, count_processed). Flips processed lines to [x] with
    today's date and writes the file (caller is responsible for the
    pathspec-restricted git commit — see control-panel plan)."""
    if not __import__("os").path.exists(config.RESEARCH_REQUESTS_PATH):
        return "", 0

    with open(config.RESEARCH_REQUESTS_PATH) as f:
        text = f.read()

    requests = [r for r in parse_requests(text) if not r["checked"]]
    if not requests:
        return "", 0

    lines = text.splitlines()
    findings_blocks = []
    today = datetime.now().strftime("%Y-%m-%d")

    async with mcp_client.McpSession() as session:
        for req in requests:
            log(f"Researching: {req['text'][:80]}")
            finding = await research_one(session, req["text"], phase_cb=phase_cb)
            findings_blocks.append(finding)
            lines[req["line_index"]] = f"- [x] {req['text']} (researched {today})"

    with open(config.RESEARCH_REQUESTS_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    return "\n\n".join(findings_blocks), len(requests)


def run_pending() -> tuple[str, int]:
    """Sync entrypoint for the CLI, with the retry-tolerant lock (same
    posture as collector.py — a daily run should wait briefly rather than
    skip research outright if the dashboard is mid-job)."""
    with mcp_client.mcp_lock(retry_seconds=120):
        return asyncio.run(run_pending_async())


if __name__ == "__main__":
    findings, count = run_pending()
    log(f"Processed {count} request(s)")
    if findings:
        print(findings[:2000])
