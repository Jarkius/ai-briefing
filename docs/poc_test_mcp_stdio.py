#!/usr/bin/env python3
"""POC harness for noapi-google-search-mcp.

Spawns the MCP server over stdio (same way Claude Desktop / an MCP client
would) and exercises the tools relevant to ai-briefing:
  1. list_tools        — confirm all 38 tools register
  2. google_search     — AI news query with past_day filter
  3. google_news       — news vertical
  4. google_lens       — reverse image search (beyond-text capability)
  5. transcribe_video  — YouTube transcription (beyond-text capability)

Usage: .venv/bin/python poc_test.py [--skip-transcribe]
"""

import asyncio
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(
    command=sys.executable,
    args=["-m", "google_search_mcp"],
)

# Short, stable public video (jawed's "Me at the zoo", 19s) keeps the
# whisper step fast for a POC.
TRANSCRIBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
LENS_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Camponotus_flavomarginatus_ant.jpg/640px-Camponotus_flavomarginatus_ant.jpg"


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


def show(result, limit=1200):
    for block in result.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            print(text[:limit] + ("\n… [truncated]" if len(text) > limit else ""))
        else:
            print(f"[non-text content: {getattr(block, 'type', '?')}]")


async def run_tool(session, name, args, timeout):
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout)
        elapsed = time.monotonic() - start
        status = "ERROR" if result.isError else "OK"
        print(f"[{status}] {name} in {elapsed:.1f}s")
        show(result)
        return not result.isError
    except asyncio.TimeoutError:
        print(f"[TIMEOUT] {name} after {timeout}s")
        return False
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        return False


async def main():
    skip_transcribe = "--skip-transcribe" in sys.argv
    passed = {}

    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            banner("1. TOOL DISCOVERY")
            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"{len(tools)} tools registered:")
            print(", ".join(names))
            passed["discovery"] = len(tools) >= 30

            banner("2. GOOGLE WEB SEARCH (past_day, AI news)")
            passed["google_search"] = await run_tool(
                session, "google_search",
                {"query": "AI LLM news announcements", "num_results": 5,
                 "time_range": "past_day"},
                timeout=120,
            )

            banner("3. GOOGLE NEWS")
            passed["google_news"] = await run_tool(
                session, "google_news",
                {"query": "artificial intelligence", "num_results": 5},
                timeout=120,
            )

            banner("4. GOOGLE LENS (reverse image search)")
            passed["google_lens"] = await run_tool(
                session, "google_lens",
                {"image_source": LENS_IMAGE_URL},
                timeout=180,
            )

            if skip_transcribe:
                print("\n[skipped] transcribe_video (--skip-transcribe)")
            else:
                banner("5. YOUTUBE TRANSCRIPTION (19s video)")
                passed["transcribe_video"] = await run_tool(
                    session, "transcribe_video",
                    {"url": TRANSCRIBE_URL},
                    timeout=600,
                )

    banner("POC SUMMARY")
    for name, ok in passed.items():
        print(f"  {'✓' if ok else '✗'} {name}")
    if all(passed.values()):
        print("\nALL POC CHECKS PASSED")
        return 0
    print("\nSOME CHECKS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
