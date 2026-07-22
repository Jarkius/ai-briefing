# POC: noapi-google-search-mcp

**Date:** 2026-07-22
**Repo tested:** [VincentKaufmann/noapi-google-search-mcp](https://github.com/VincentKaufmann/noapi-google-search-mcp) v0.3.1 (114 ⭐, MIT, Python)
**Local clone:** `~/workspace/lab/noapi-google-search-mcp` (venv `.venv`, Python 3.11)
**Test harnesses:** `poc_test.py`, `poc_test2.py` in the clone — spawn the server over MCP stdio and exercise tools end-to-end.

## What it is

An MCP server bundling 38 tools with zero API keys: Google search verticals
(web/news/scholar/images/trends/shopping/flights/hotels/maps/finance/weather/books),
Google Lens reverse image search, local OCR (RapidOCR), YouTube + local media
transcription (faster-whisper), AI clip extraction (yt-dlp + ffmpeg), feed
subscriptions with SQLite FTS5 full-text search (news RSS / Reddit / HN /
GitHub / arXiv / YouTube / podcasts / Twitter), document reading (PDF/DOCX/…),
page fetching via headless Chromium, Wikipedia, IMAP email, and utilities.

## Setup notes

- **Python <3.13 required** (`rapidocr-onnxruntime` pin). System Python 3.14
  fails; used `uv venv --python 3.11`.
- `playwright install chromium` needed after pip install (~94 MB).
- Whisper `tiny` model (~75 MB) auto-downloads from HuggingFace on first
  transcription.

## Results

| Tool | Result | Notes |
|------|--------|-------|
| Tool discovery | ✅ | All 38 tools register over MCP stdio |
| `transcribe_video` (YouTube) | ✅ | Accurate timestamped transcript of test video; disk-cached (instant on repeat) |
| `wikipedia` | ✅ | Clean article summary |
| `visit_page` | ✅ | Fetched HN front page, readable text extraction via Chromium |
| `subscribe` / `check_feeds` / `get_feed_items` | ✅ | HN top: 30 items fetched into SQLite, browsable + FTS-searchable |
| `google_news` | ⚠️ | Returns transport-OK but empty content from this network |
| `google_search` | ❌ | "Blocked by Google bot detection" — persisted across retries |
| `google_lens` | ❌ | Google serves "unusual traffic" interstitial instead of results |

## Bug found & fixed locally

`yt-dlp`'s progress bar writes to **stdout**, which corrupts the MCP stdio
JSON-RPC channel — first `transcribe_video` call hung until timeout with
`Failed to parse JSONRPC message` (progress bytes interleaved with the
response). Fix: add `"noprogress": True` to both `ydl_opts` dicts in
`server.py` (patched in local clone; worth a PR upstream).

## Key caveat: Google bot detection

All Google-branded tools are currently blocked from this network/IP
(residential IP got the "unusual traffic" page consistently). The project
ships stealth patches + cookie persistence + a CAPTCHA solver, so results may
improve after warm-up or from another network — but **Google tools must be
treated as best-effort, not a reliable pipeline dependency**. Notably this is
the same class of network flakiness that broke the 6am briefing job.

The non-Google capabilities worked flawlessly and are the durable value:

- **YouTube transcription / RAG** (subscribe → auto-transcribe → FTS search →
  clip extraction) — genuinely beyond-text, fully local, no quota.
- **Feed subscription layer** — could replace ai_briefing.py's hand-rolled
  HN/RSS/GitHub fetchers with cached, deduplicated, searchable storage.
- **`visit_page`** — Chromium rendering handles JS-heavy pages our
  urllib-based fetcher cannot.
- **OCR / document reading** — briefing could ingest PDFs and screenshots.

## Recommendation for ai-briefing integration

1. Adopt the **feed subscription + FTS layer** as the collector (replaces most
   of the custom fetch code and the `.seen_cache.json` dedup).
2. Add **YouTube channel subscriptions** (AI channels auto-transcribed →
   briefing gains video content — the "more than text" win).
3. Treat **Google search tools as a bonus source** with graceful fallback,
   never a critical path.
4. Upstream the `noprogress` fix; pin our own fork or vendor the server until
   merged.
