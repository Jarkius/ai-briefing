#!/usr/bin/env python3
"""POC round 2 — non-Google tools relevant to ai-briefing, plus a
google_search retry after cooldown. Checks result CONTENT, not just
transport success."""

import asyncio
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "google_search_mcp"])

BLOCK_MARKERS = ["blocked by Google bot detection", "unusual traffic", "CAPTCHA"]


def banner(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}", flush=True)


async def call(session, name, args, timeout=120):
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout)
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        return False, ""
    text = "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")
    elapsed = time.monotonic() - start
    blocked = any(m.lower() in text.lower() for m in BLOCK_MARKERS)
    ok = (not result.isError) and (not blocked) and len(text.strip()) > 40
    print(f"[{'OK' if ok else 'BLOCKED' if blocked else 'WEAK'}] {name} in {elapsed:.1f}s")
    print(text[:900] + ("\n… [truncated]" if len(text) > 900 else ""))
    return ok, text


async def main():
    passed = {}
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            banner("A. WIKIPEDIA (no Google)")
            passed["wikipedia"], _ = await call(
                session, "wikipedia", {"query": "large language model"})

            banner("B. VISIT_PAGE (fetch + extract, no Google)")
            passed["visit_page"], _ = await call(
                session, "visit_page", {"url": "https://news.ycombinator.com"})

            banner("C. FEEDS: subscribe to HackerNews top")
            passed["subscribe"], _ = await call(
                session, "subscribe", {"source_type": "hackernews", "identifier": "top"})

            banner("D. FEEDS: check_feeds")
            passed["check_feeds"], _ = await call(
                session, "check_feeds", {}, timeout=300)

            banner("E. FEEDS: get_feed_items")
            passed["get_feed_items"], _ = await call(
                session, "get_feed_items", {"limit": 5})

            banner("F. GOOGLE_SEARCH retry (after cooldown)")
            passed["google_search"], _ = await call(
                session, "google_search",
                {"query": "anthropic claude news", "num_results": 3,
                 "time_range": "past_week"})

    banner("SUMMARY")
    for k, v in passed.items():
        print(f"  {'✓' if v else '✗'} {k}")
    return 0 if all(passed.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
