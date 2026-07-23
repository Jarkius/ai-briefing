"""Generator phase: query feeds.db for recent items, budget them into a
Gemini prompt alongside newsletter_style.md, and produce the two-part
HTML briefing.

Token budget (mcp-integration plan step 7): 60,000 chars of item content
total, newest-first, with each transcript-type item capped at 8,000 chars
(head+tail) before inclusion. If still over budget, drop whole lowest-
priority items rather than truncating mid-item. Priority order: research
findings > news/HN > GitHub/arXiv > YouTube transcripts.
"""

import json
import os
import re
from datetime import datetime

from . import config, db, sender

TOTAL_CHAR_BUDGET = 60_000
TRANSCRIPT_CHAR_CAP = 8_000
# Hard cap on item *count*, independent of the char budget. The maxplus
# backend times out (~80s wall clock) generating a full formatted entry —
# headline, summary, social post, hashtags, source — per item; this is an
# OUTPUT generation time limit, not an input size limit (confirmed: 10 real
# items took 73s and produced 11k chars of output; instructions alone with
# no data returned in 9s). Measured empirically per Gemini call — each of
# the 6 section calls covers 2 subsections, so this cap must be low enough
# that a single call's worth of items (not the full budgeted set) stays
# safely under the timeout. 8 items per section call, spread across 6 calls
# via round-robin in call_gemini, keeps each individual call's generation
# time well clear of the ~80s wall.
MAX_ITEMS_PER_SECTION_CALL = 8
MAX_ITEMS_TOTAL = MAX_ITEMS_PER_SECTION_CALL * 6  # one slice per SECTION_PROMPTS call

# Lower number = higher priority = dropped last when over budget.
SOURCE_PRIORITY = {
    "research": 0,
    "hackernews": 1,
    "news": 1,
    "github": 2,
    "arxiv": 2,
    "reddit": 2,
    "youtube": 3,
    "podcast": 3,
    "twitter": 2,
}

SYSTEM_PROMPT = (
    "You are a senior AI awareness editor writing for business leaders, educators, and "
    "professionals learning to work with AI. Focus on practical skills, security best practices, "
    "prompt engineering tips, AI literacy, and ethical use. Write in plain language with actionable "
    "takeaways. Every item needs a paragraph summary and a shareable social post (≤280 chars)."
)

BASE_RULES = "Rules: factual, plain language, mark rumours, include all source links from the data."

SECTION_PROMPTS = [
    ("1/6: top stories + news", """Write these two sections in Markdown:

## 🔥 Top 3 Stories This Briefing
Three most impactful items. For each story:
**Headline**
What happened (2-3 sentences in plain language)
**Why it matters:** (1 clear sentence)
📱 Social post: (≤280 chars, include 2-3 hashtags)
[Source](url)

## 📰 AI News & Headlines
For each news item:
**Bold headline**
Paragraph explaining what happened (3-4 sentences for non-experts)
**Key takeaway:** (actionable insight, 1 sentence)
📱 Social post: (≤280 chars with hashtags)
[Source](url)"""),
    ("2/6: governance + mindset", """Write these two sections in Markdown:

## 🏛️ AI Governance & Policy
Regulation, ethics, safety, company policies, government moves.
For each:
**Bold topic**
Paragraph explaining the policy/regulation (3-4 sentences)
**Key takeaway:** (what it means for practitioners)
📱 Social post: (≤280 chars with hashtags)
[Source](url)

## 🧠 AI Mindset & Culture
How AI is changing work, thinking, and collaboration. Human-interest angles.
For each:
**Bold headline**
What's happening (3-4 sentences)
**Key takeaway:** (actionable insight)
📱 Social post: (≤280 chars with hashtags)
[Source](url)"""),
    ("3/6: learning + best practices", """Write these two sections in Markdown:

## 📚 AI Learning & Best Practices
Tutorials, workflows, case studies, how-tos from the data.
For each:
**Bold title**
What you'll learn (3-4 sentences, beginner-friendly)
**Key takeaway:** (why this matters)
📱 Social post: (≤280 chars with hashtags like #AILearning #Tutorial)
[Source](url)

## 🎯 Prompt Engineering Tips
Effective prompting techniques, examples, patterns from the data.
For each tip:
**Bold technique name**
How it works (2-3 sentences with example)
**Key takeaway:** (when to use this)
📱 Social post: (≤280 chars with hashtags like #PromptEngineering #AITips)
[Source](url)"""),
    ("4/6: security + ethics", """Write these two sections in Markdown:

## 🔒 AI Security & Privacy
Security risks, vulnerabilities, data protection, safe AI practices from the data.
For each:
**Bold topic**
The security issue explained (3 sentences)
**Action to take:** (1-2 concrete steps)
📱 Social post: (≤280 chars with hashtags like #AISecurity #Privacy)
[Source](url)

## ⚖️ AI Ethics & Responsible Use
Bias, fairness, transparency, accountability issues from the data.
For each:
**Bold topic**
The ethical issue (3 sentences)
**What to consider:** (guidance for practitioners)
📱 Social post: (≤280 chars with hashtags like #AIEthics #ResponsibleAI)
[Source](url)"""),
    ("5/6: research + tools", """Write these two sections in Markdown:

## 🔬 AI Research & Emerging Capabilities
New research, papers, experimental capabilities from the data.
For each:
**Bold headline**
What was discovered/built (3-4 sentences, accessible language)
**Why it matters:** (implications for practitioners)
📱 Social post: (≤280 chars with hashtags like #AIResearch #MachineLearning)
[Source](url)

## 💻 Useful AI Tools & Resources
GitHub repos, frameworks, libraries, datasets from the data.
For each tool:
**Bold tool name** (⭐ star count if GitHub)
What it does (2-3 sentences)
**Key feature:** (standout capability)
📱 Social post: (≤280 chars with hashtags like #AITools #OpenSource)
[Source](url)"""),
    ("6/6: community conversations", """Write this section in Markdown:

## 💬 Community Conversations
Hot topics, debates, discussions from HackerNews and Reddit in the data.
For each:
**Bold topic**
What the community is discussing (3-4 sentences)
**Key insight:** (takeaway from the discussion)
📱 Social post: (≤280 chars with hashtags like #AI #TechTwitter #HackerNews)
[Source](url)"""),
]


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _grok_call(system: str, user: str, max_attempts: int = 4) -> str:
    """Single Gemini call via maxplus, max 4000 output tokens.

    Retries with exponential backoff — the maxplus pool has been observed
    to return intermittent HTTP 503 "Service Unavailable" independent of
    payload size (a 30k-char prompt failed twice in a row, then a 50k-char
    prompt with the same structure succeeded immediately after), so this is
    provider-side flakiness, not a deterministic size limit. Two immediate
    attempts with no delay (the original behavior) wasn't enough to ride
    out a transient backend blip."""
    import urllib.error
    import urllib.request

    payload = {
        "model": config.MAXPLUS_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        "https://api.maxplus-ai.cc/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.MAXPLUS_API_KEY}",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            log(f"  Gemini call failed (attempt {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                import time
                backoff = 10 * (2 ** attempt)
                log(f"  retrying in {backoff}s…")
                time.sleep(backoff)
    raise last_error


def fetch_recent_items(conn, since_hours: int = 24) -> list[dict]:
    """Items from feed_items fetched in the last N hours, newest first."""
    db.assert_feed_items_schema(conn)
    rows = conn.execute(
        """SELECT title, content, url, source_type, published_at, fetched_at
           FROM feed_items
           WHERE fetched_at >= datetime('now', ?)
           ORDER BY fetched_at DESC""",
        (f"-{since_hours} hours",),
    ).fetchall()
    return [dict(row) for row in rows]


def _cap_transcript(item: dict) -> dict:
    content = item.get("content", "")
    if item.get("source_type") == "youtube" and len(content) > TRANSCRIPT_CHAR_CAP:
        half = TRANSCRIPT_CHAR_CAP // 2
        item = dict(item)
        item["content"] = (
            content[:half] + "\n...[transcript truncated]...\n" + content[-half:]
        )
    return item


def _rendered_size(item: dict) -> int:
    """Approximate the chars this item contributes once build_context
    serializes it (title + url + content + formatting overhead) — must
    track build_context's actual format or the budget silently undercounts."""
    overhead = 12  # "- ", " — ", "\n  ", newlines
    return len(item.get("title", "")) + len(item.get("url", "")) + len(item.get("content", "")[:2000]) + overhead


def budget_items(items: list[dict], extra_budget_used: int = 0, max_items: int = MAX_ITEMS_TOTAL) -> list[dict]:
    """Apply the hard char budget AND the hard item-count cap: cap
    transcripts, then drop whole lowest-priority items (by SOURCE_PRIORITY,
    newest-first within a priority tier) until under both limits."""
    capped = [_cap_transcript(item) for item in items]
    remaining_budget = TOTAL_CHAR_BUDGET - extra_budget_used
    running_total = 0
    by_priority = sorted(
        enumerate(capped),
        key=lambda pair: SOURCE_PRIORITY.get(pair[1].get("source_type", ""), 5),
    )
    keep_indices = set()
    for idx, item in by_priority:
        if len(keep_indices) >= max_items:
            break
        size = _rendered_size(item)
        if running_total + size > remaining_budget and keep_indices:
            continue  # drop this item, but keep evaluating smaller/higher-priority ones
        running_total += size
        keep_indices.add(idx)
    return [capped[i] for i in sorted(keep_indices)]


def build_context(items: list[dict]) -> str:
    lines = []
    by_type: dict[str, list[dict]] = {}
    for item in items:
        by_type.setdefault(item.get("source_type", "unknown"), []).append(item)
    for source_type, group in by_type.items():
        lines.append(f"=== {source_type.upper()} ===")
        for item in group:
            title = item.get("title", "")
            url = item.get("url", "")
            # markdown-link format, not a bare trailing URL: _sanitize()'s
            # URL-stripping regex only spares URLs already inside
            # [text](url) — a bare "- title — url" line loses its link
            # entirely, which is what silently dropped every source link
            # before this fix (Gemini's output resorted to "URL not
            # provided in raw data" for every item).
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
            if item.get("content"):
                lines.append(f"  {item['content'][:2000]}")
        lines.append("")
    return "\n".join(lines)


def _sanitize(text: str) -> str:
    sanitized = re.sub(r'(?<!\()(https?://[^\s\)]+)(?!\))', '', text)
    sanitized = re.sub(r'\b(exploit|0-day|hack|bypass|malware)\b', '[security-related]', sanitized, flags=re.IGNORECASE)
    return sanitized


def call_gemini(items: list[dict], date: str, style: str, research_findings: str = "") -> str:
    """Six focused Gemini calls, same structure as the original
    ai_briefing.py:call_ai, extended with newsletter_style.md appended
    verbatim to every call so style edits shape every section.

    Each call gets its OWN slice of up to MAX_ITEMS_PER_SECTION_CALL items
    (round-robin across the budgeted pool), not the full shared context —
    the maxplus backend times out generating full formatted entries for too
    many items in one call (measured: 10 items ~73s, close to its ~80s
    limit), so keeping each individual call's item count low keeps
    generation time safely under that wall. Research findings, being the
    highest-priority content, are included in every call rather than just
    one slice."""
    style_block = f"\n\nSTYLE RULES (follow these exactly):\n{style}" if style.strip() else ""
    research_block = ""
    if research_findings:
        research_block = f"\n\n=== REQUESTED RESEARCH ===\n{_sanitize(research_findings)}\n"

    parts = []
    for i, (label, instructions) in enumerate(SECTION_PROMPTS):
        slice_items = items[i::len(SECTION_PROMPTS)][:MAX_ITEMS_PER_SECTION_CALL]
        context = _sanitize(build_context(slice_items))
        ctx = f"Today is {date}.{research_block}\n\nRAW DATA:\n{context}"

        log(f"Calling Gemini ({label}, {len(slice_items)} items)…")
        parts.append(_grok_call(
            SYSTEM_PROMPT,
            f"{ctx}\n\n{instructions}\n\n{BASE_RULES}{style_block}",
        ))
    return "\n\n---\n\n".join(parts)


def generate(conn, research_findings: str = "") -> dict:
    """Run the full generate step: fetch -> budget -> Gemini -> HTML.
    Returns {'markdown': str, 'part1_html': str, 'part2_html': str,
    'date_str': str, 'today': str}. Callers (CLI and dashboard) decide what
    to do with the result (archive + send, or just render for preview)."""
    today = datetime.now().strftime("%Y-%m-%d")
    date_str = datetime.now().strftime("%A, %B %-d, %Y")

    items = fetch_recent_items(conn)
    research_budget_used = len(research_findings)
    items = budget_items(items, extra_budget_used=research_budget_used)
    log(f"Budgeted {len(items)} items into the prompt (research findings: {research_budget_used} chars)")

    style = config.load_style()

    markdown = call_gemini(items, date_str, style, research_findings=research_findings)

    part1_md, part2_md = sender.split_two_parts(markdown)
    part1_html = sender.markdown_to_html(part1_md, date_str, title="Daily AI Briefing — Part 1")
    part2_html = sender.markdown_to_html(part2_md, date_str, title="Daily AI Briefing — Part 2")

    os.makedirs(config.ARCHIVE_DIR, exist_ok=True)
    archive_path = os.path.join(config.ARCHIVE_DIR, f"briefing_{today}_{datetime.now().strftime('%H%M')}.md")
    with open(archive_path, "w") as f:
        f.write(markdown)
    log(f"Archived to {archive_path}")

    return {
        "markdown": markdown,
        "part1_html": part1_html,
        "part2_html": part2_html,
        "date_str": date_str,
        "today": today,
    }


if __name__ == "__main__":
    conn = db.connect()
    result = generate(conn)
    print(result["part1_html"][:500])
    conn.close()
