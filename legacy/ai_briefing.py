#!/usr/bin/env python3
"""
Daily AI Briefing
1. Fetches live AI news from HackerNews API + RSS feeds
2. Summarizes with Claude (claude-sonnet-5) via maxplus API
3. Emails result via Gmail SMTP (recipient configured in .env)
Run:       python3 ai_briefing.py
Scheduled: launchd via com.user.ai-briefing.plist
"""

import json, re, sys, smtplib, os, time, subprocess
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Force UTF-8 output so emoji/arrows in logs don't crash on Windows consoles
# using legacy code pages (e.g. cp874 on Thai-locale Windows).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── LOAD .env (if present) ─────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
MAXPLUS_API_KEY    = os.environ.get("MAXPLUS_API_KEY", "")
MAXPLUS_MODEL      = os.environ.get("MAXPLUS_MODEL",   "gemini-3.5-flash")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY",  "")
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL",    "gemini-3.5-flash")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",   "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

_missing = [name for name, val in [
    ("GMAIL_ADDRESS", GMAIL_ADDRESS),
    ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD),
] if not val]
if _missing:
    sys.exit(f"ERROR — missing config: {', '.join(_missing)}. Copy .env.example to .env and fill it in.")

# Browser-like User-Agent so Reddit and other anti-bot hosts don't 429/403 us
USER_AGENT = "Mozilla/5.0 (compatible; Jarkius-AIBriefing/1.0; +https://jarkius.local)"

# Deduplication cache (stores URLs seen in last 7 days), kept next to this script
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_cache.json")
CACHE_DAYS = 7  # Only show items not seen in last 7 days

RSS_FEEDS = [
    # ── Official Frontier Labs & Infrastructure (primary sources) ──────────────
    # NOTE: Anthropic discontinued its native RSS feed (404) — covered via HN/news feeds instead.
    "https://openai.com/news/rss.xml",                              # OpenAI Blog
    "https://deepmind.google/blog/rss.xml",                         # Google DeepMind
    "https://blog.google/technology/ai/rss/",                       # Google AI blog
    "https://huggingface.co/blog/feed.xml",                         # Hugging Face blog
    "https://blog.langchain.dev/rss/",                              # LangChain blog
    "https://developer.nvidia.com/blog/feed/",                      # NVIDIA technical blog
    "https://pytorch.org/feed.xml",                                 # PyTorch official blog

    # ── Technical Newsletters & Editorials ─────────────────────────────────────
    "https://www.latent.space/feed",                               # Latent Space (Swyx & Alessio)
    "https://aheadofai.substack.com/feed",                         # Ahead of AI (Sebastian Raschka)
    "https://importai.substack.com/feed",                          # Import AI (Jack Clark)
    "https://rss.beehiiv.com/feeds/tl-dr-ai.xml",                  # TLDR AI
    "https://www.deeplearning.ai/the-batch/feed/",                 # DeepLearning.AI - The Batch

    # ── General AI News / Media ────────────────────────────────────────────────
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.artificialintelligence-news.com/feed/",           # AI News
    "https://www.marktechpost.com/feed/",                           # ML/AI research & tutorials

    # ── Developer Communities & Firehose ───────────────────────────────────────
    "https://www.reddit.com/r/LocalLLaMA/hot.rss?limit=15",        # r/LocalLLaMA (local inference, quant, hardware)
    "https://www.reddit.com/r/MachineLearning/hot.rss?limit=10",   # r/MachineLearning (papers, benchmarks)
    "https://hnrss.org/frontpage?q=LLM+OR+AI&points=50",           # HackerNews AI, filtered 50+ points
    # NOTE: github-trending-rss.glitch.me is dead (410 Gone). GitHub trending is
    # already covered natively by fetch_github_trending(), so no RSS bridge needed.

    # ── Research Papers (arXiv) ────────────────────────────────────────────────
    "http://export.arxiv.org/rss/cs.CL",                            # Computation & Language (LLM papers)
    "http://export.arxiv.org/rss/cs.AI",                            # Artificial Intelligence (general)
    "http://export.arxiv.org/rss/cs.LG",                            # Machine Learning
]

AI_KEYWORDS = [
    "ai", "llm", "gpt", "claude", "openai", "anthropic", "gemini",
    "deepseek", "mistral", "ollama", "machine learning", "neural",
    "model", "agent", "diffusion", "robot", "copilot", "chatgpt",
    "sora", "midjourney", "stable diffusion", "hugging face",
    "prompt", "prompting", "rag", "fine-tuning", "embedding",
    "security", "privacy", "ethics", "alignment", "safety",
    "tutorial", "guide", "best practice", "workflow", "automation",
    "vector", "langchain", "llama", "transformer",
]
# ───────────────────────────────────────────────────────────────────────────────


# ── NEWS FETCHING ──────────────────────────────────────────────────────────────

def fetch_hackernews(max_scan: int = 60) -> list[dict]:
    """Return top AI-related HN stories, sorted by score."""
    try:
        with urllib.request.urlopen(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        ) as r:
            ids = json.loads(r.read())[:max_scan]
    except Exception as e:
        log(f"  HackerNews fetch failed: {e}")
        return []

    stories = []
    for sid in ids:
        try:
            with urllib.request.urlopen(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=5
            ) as r:
                s = json.loads(r.read())
            if not s or "title" not in s:
                continue
            if any(kw in s["title"].lower() for kw in AI_KEYWORDS):
                stories.append({
                    "title":    s["title"],
                    "url":      s.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                    "score":    s.get("score", 0),
                    "comments": s.get("descendants", 0),
                    "hn_url":   f"https://news.ycombinator.com/item?id={sid}",
                })
        except Exception:
            continue

    return sorted(stories, key=lambda x: x["score"], reverse=True)[:12]


def fetch_github_trending() -> list[dict]:
    """
    Fetch trending AI/ML repos via GitHub Search API. Searches ALL repos (no date filter),
    deduplication cache prevents sending same repos within 7 days. This way users discover
    important older repos they may have missed, not just brand-new ones.
    """
    from urllib.parse import urlencode

    # Search for repos with educational/practical value across all time
    queries = [
        "topic:artificial-intelligence topic:llm stars:>500",
        "topic:gpt topic:chatgpt stars:>500",
        "topic:ai-tools topic:productivity stars:>200",
        "topic:machine-learning topic:education stars:>200",
        "prompt engineering stars:>100",
        "ai security stars:>100",
        "llm best-practices stars:>100",
    ]

    all_repos = []
    for query in queries:
        params = urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": 10})
        url = f"https://api.github.com/search/repositories?{params}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())

            for repo in data.get("items", []):
                # Include repos with 50+ stars (lower threshold for educational content)
                if repo.get("stargazers_count", 0) >= 50:
                    all_repos.append({
                        "name":        repo["full_name"],
                        "url":         repo["html_url"],
                        "description": repo.get("description", "No description"),
                        "stars":       repo["stargazers_count"],
                        "language":    repo.get("language", ""),
                    })
        except Exception as e:
            log(f"  GitHub query '{query[:40]}...' failed: {e}")
            continue

    # Deduplicate by URL and return top 15
    seen = set()
    unique = []
    for repo in all_repos:
        if repo["url"] not in seen:
            seen.add(repo["url"])
            unique.append(repo)

    return sorted(unique, key=lambda x: x["stars"], reverse=True)[:15]


def fetch_reddit_ai() -> list[dict]:
    """
    Reddit consistently blocks scraping (403) without OAuth.
    Would need official Reddit API with app credentials.
    Disabled for now - use Twitter/ProductHunt for community buzz instead.
    """
    return []


def fetch_twitter_ai_trends() -> list[dict]:
    """
    Fetch AI trending topics from Twitter/X via Nitter (public mirror).
    Falls back gracefully if blocked.
    """
    trends = []
    try:
        # Nitter instance - public Twitter mirror
        req = urllib.request.Request(
            "https://nitter.net/search?f=tweets&q=%23AI+OR+%23MachineLearning+OR+%23LLM+OR+%23ChatGPT+OR+%23Claude+min_retweets%3A50&since=24h",
            headers={"User-Agent": "AI-Briefing/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode('utf-8')

        # Basic scraping - look for tweet content
        tweet_pattern = r'<div class="tweet-content[^"]*">(.*?)</div>'
        matches = re.findall(tweet_pattern, html, re.DOTALL)[:10]

        for i, content in enumerate(matches):
            # Strip HTML tags
            clean = re.sub(r'<[^>]+>', '', content).strip()
            if len(clean) > 50 and any(kw in clean.lower() for kw in AI_KEYWORDS):
                trends.append({
                    "text": clean[:280],
                    "source": "X/Twitter"
                })
    except Exception as e:
        log(f"  Twitter/X trends fetch failed (not critical): {e}")

    return trends


def fetch_producthunt_ai() -> list[dict]:
    """Fetch trending AI products from Product Hunt."""
    products = []
    try:
        req = urllib.request.Request(
            "https://www.producthunt.com/topics/artificial-intelligence",
            headers={"User-Agent": "AI-Briefing/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode('utf-8')

        # Basic scraping for product names
        name_pattern = r'data-test="post-name"[^>]*>([^<]+)</a>'
        names = re.findall(name_pattern, html)[:8]

        for name in names:
            if any(kw in name.lower() for kw in ["ai", "llm", "gpt", "ml", "agent", "chat"]):
                products.append({
                    "name": name.strip(),
                    "url": f"https://www.producthunt.com/search?q={name.replace(' ', '+')}",
                })
    except Exception as e:
        log(f"  ProductHunt fetch failed (not critical): {e}")

    return products[:5]


def fetch_rss(url: str, limit: int = 6) -> list[dict]:
    """
    Fetch and parse an RSS 2.0 or Atom feed with robust error handling.
    Handles malformed XML by catching parse errors gracefully.
    """
    host = url.split('/')[2] if '//' in url else url
    # Browser-like headers: Reddit (429) and others reject default urllib agents.
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    raw_xml = None
    # Retry once on transient errors (429 rate-limit, 500/502/503 gateway hiccups
    # — e.g. hnrss.org 502, LangChain Ghost CMS truncation).
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw_xml = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt == 1:
                log(f"  RSS {host} HTTP {e.code} — retrying in 3s…")
                time.sleep(3)
                continue
            log(f"  RSS {host} failed: HTTP {e.code}")
            return []
        except Exception as e:
            if attempt == 1:
                log(f"  RSS {host} error ({e}) — retrying in 3s…")
                time.sleep(3)
                continue
            log(f"  RSS {host} failed: {e}")
            return []

    if raw_xml is None:
        return []

    # Try parsing with ET - catches most malformed XML
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        log(f"  RSS {host} failed: XML parse error - {e}")
        return []

    items = []
    # RSS 2.0
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        if title:
            items.append({"title": title, "url": link, "desc": desc[:250]})
    # Atom
    if not items:
        ns = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f".//{{{ns}}}entry")[:limit]:
            title   = (entry.findtext(f"{{{ns}}}title") or "").strip()
            link_el = entry.find(f"{{{ns}}}link")
            link    = link_el.get("href", "") if link_el is not None else ""
            summary = re.sub(r"<[^>]+>", "",
                             entry.findtext(f"{{{ns}}}summary") or "").strip()
            if title:
                items.append({"title": title, "url": link, "desc": summary[:250]})
    return items


# ── AI SUMMARISATION (Grok via maxplus) ────────────────────────────────────────

def _claude_cli_call(system: str, user: str) -> str:
    """Call Claude via `claude -p` CLI, piping prompt via stdin to avoid arg-length limits."""
    prompt = f"{system}\n\n{user}"
    result = subprocess.run(
        ["claude", "-p", "--model", "opus", "-"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _gemini_direct_call(system: str, user: str) -> str:
    """Call Google Gemini directly via REST API (fallback #1)."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{user}"}]}],
        "generationConfig": {"maxOutputTokens": 4000},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    # Thinking models include parts without "text" (e.g. thoughtSignature) — skip them
    parts = data["candidates"][0]["content"]["parts"]
    return next(p["text"] for p in parts if "text" in p)


def _maxplus_call(system: str, user: str) -> str:
    """Call Gemini via MaxPlus API (fallback #2)."""
    payload = {
        "model": MAXPLUS_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        "https://api.maxplus-ai.cc/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {MAXPLUS_API_KEY}",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


_claude_cli_disabled = False  # set True after first cert failure to skip on subsequent calls

def _grok_call(system: str, user: str) -> str:
    """Try Claude CLI → direct Gemini API → MaxPlus Gemini (in order)."""
    global _claude_cli_disabled
    if not _claude_cli_disabled:
        try:
            return _claude_cli_call(system, user)
        except Exception as e:
            err = str(e)[:120]
            log(f"  Claude CLI failed ({err}) — trying direct Gemini API…")
            if "nscacert" in err or "exit 3" in err or "exited 3" in err:
                _claude_cli_disabled = True
                log("  Claude CLI disabled for this run (cert issue)")
    try:
        return _gemini_direct_call(system, user)
    except Exception as e:
        log(f"  Direct Gemini failed ({str(e)[:120]}) — falling back to MaxPlus…")
        return _maxplus_call(system, user)


def call_ai(raw_data: str, date: str) -> str:
    """Six focused Gemini calls to generate comprehensive briefing."""

    system = (
        "You are a senior AI awareness editor writing for business leaders, educators, and "
        "professionals learning to work with AI. Focus on practical skills, security best practices, "
        "prompt engineering tips, AI literacy, and ethical use. Write in plain language with actionable "
        "takeaways. Every item needs a paragraph summary and a shareable social post (≤280 chars)."
    )

    # Aggressive sanitization to avoid Grok safety blocks:
    # 1. Strip all standalone URLs (keeps markdown [title](url) intact)
    # 2. Remove security/exploit keywords that trigger filters
    sanitized = re.sub(r'(?<!\()(https?://[^\s\)]+)(?!\))', '', raw_data)
    sanitized = re.sub(r'\b(exploit|0-day|hack|bypass|malware)\b', '[security-related]', sanitized, flags=re.IGNORECASE)

    # DEBUG: write sanitized payload (relative to this script; never fatal)
    try:
        _debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_sanitized.txt")
        with open(_debug_path, "w", encoding="utf-8") as f:
            f.write(sanitized)
    except Exception as _e:
        log(f"  (debug write skipped: {_e})")

    ctx = f"Today is {date}.\n\nRAW DATA (HackerNews, RSS, GitHub, Reddit):\n{sanitized}"
    base_rules = "Rules: factual, plain language, mark rumours, include all source links from the data."

    # ── CALL 1: Top stories + News ────────────────────────────────────────────
    log("Calling Gemini (1/6: top stories + news)…")
    part1 = _grok_call(system, f"""{ctx}

Write these two sections in Markdown:

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
[Source](url)

{base_rules}""")

    # ── CALL 2: Governance + Mindset ─────────────────────────────────────────
    log("Calling Gemini (2/6: governance + mindset)…")
    part2 = _grok_call(system, f"""{ctx}

Write these two sections in Markdown:

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
[Source](url)

{base_rules}""")

    # ── CALL 3: Learning + Best Practices ──────────────────────────────────────
    log("Calling Gemini (3/6: learning + best practices)…")
    part3 = _grok_call(system, f"""{ctx}

Write these two sections in Markdown:

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
[Source](url)

{base_rules}""")

    # ── CALL 4: Security + Ethics ──────────────────────────────────────────────
    log("Calling Gemini (4/6: security + ethics)…")
    part4 = _grok_call(system, f"""{ctx}

Write these two sections in Markdown:

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
[Source](url)

{base_rules}""")

    # ── CALL 5: Research + Tools ──────────────────────────────────────────────
    log("Calling Gemini (5/6: research + tools)…")
    part5 = _grok_call(system, f"""{ctx}

Write these two sections in Markdown:

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
[Source](url)

{base_rules}""")

    # ── CALL 6: Community ────────────────────────────────────────────────────
    log("Calling Gemini (6/6: community conversations)…")
    part6 = _grok_call(system, f"""{ctx}

Write this section in Markdown:

## 💬 Community Conversations
Hot topics, debates, discussions from HackerNews and Reddit in the data.
For each:
**Bold topic**
What the community is discussing (3-4 sentences)
**Key insight:** (takeaway from the discussion)
📱 Social post: (≤280 chars with hashtags like #AI #TechTwitter #HackerNews)
[Source](url)

{base_rules}""")

    return "\n\n---\n\n".join([part1, part2, part3, part4, part5, part6])


# ── MARKDOWN → HTML ────────────────────────────────────────────────────────────

def _inline(text: str) -> str:
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  r'<a href="\2" style="color:#26890D">\1</a>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*',     r'<em>\1</em>',         text)
    return text


# Section → (tag label, tag colour, accent colour)
_SECTION_STYLES = {
    "top 3":       ("🔥 Top Stories",        "#D04A02", "#D04A02"),
    "news":        ("📰 AI News",             "#00A3E0", "#00A3E0"),
    "governance":  ("🏛️ Governance & Policy", "#6B21A8", "#6B21A8"),
    "mindset":     ("🧠 Mindset & Culture",   "#0369A1", "#0369A1"),
    "learning":    ("📚 Learning",             "#26890D", "#26890D"),
    "prompt":      ("🎯 Prompt Engineering",  "#0D9488", "#0D9488"),
    "security":    ("🔒 Security & Privacy",  "#B91C1C", "#B91C1C"),
    "ethics":      ("⚖️ Ethics",              "#92400E", "#B45309"),
    "research":    ("🔬 Research",             "#1D4ED8", "#1D4ED8"),
    "tools":       ("💻 Tools & Resources",   "#26890D", "#26890D"),
    "community":   ("💬 Community",           "#5B21B6", "#5B21B6"),
}

def _section_style(header_text: str):
    h = header_text.lower()
    for key, val in _SECTION_STYLES.items():
        if key in h:
            return val
    return ("📋 Briefing", "#26890D", "#26890D")


def markdown_to_html(text: str, date_str: str) -> str:
    """Convert briefing markdown to AI Pulse-style newsletter HTML."""
    lines = text.split("\n")
    articles = []   # list of rendered <tr> blocks
    i = 0
    current_section = ("📋 Briefing", "#26890D", "#26890D")

    while i < len(lines):
        s = lines[i].strip()

        # ── Section header → update current tag style, emit section divider row
        if s.startswith("## "):
            header_text = s[3:]
            current_section = _section_style(header_text)
            tag_label, tag_color, accent = current_section
            articles.append(f"""
  <tr><td style="padding:20px 28px 0 28px;">
    <p style="margin:0 0 4pt 0;font-size:8pt;font-weight:bold;letter-spacing:1pt;
              text-transform:uppercase;display:inline-block;padding:3px 10px;
              border-radius:2px;color:white;background:{tag_color};">&#9632; {tag_label}</p>
    <h2 style="margin:6pt 0 0 0;font-size:13pt;font-weight:bold;color:{accent};
               text-transform:uppercase;letter-spacing:.3pt;border-bottom:2px solid {accent};
               padding-bottom:6pt;">{_inline(header_text)}</h2>
  </td></tr>""")

        # ── Story card (bold headline)
        elif s.startswith("**") and "**" in s[2:]:
            end_idx = s.index("**", 2)
            headline = s[2:end_idx]
            rest = s[end_idx+2:].strip()
            _, _, accent = current_section

            body_lines = [rest] if rest else []
            i += 1
            while i < len(lines):
                nl = lines[i].strip()
                if nl.startswith(("**", "## ", "---")) or (nl == "" and i + 1 < len(lines) and lines[i+1].strip().startswith("**")):
                    break
                body_lines.append(nl)
                i += 1
            i -= 1

            body_html = ""
            for bl in body_lines:
                if not bl:
                    continue
                # Key takeaway / Why it matters → green-border callout
                if re.match(r'\*\*(Key takeaway|Why it matters|Action to take|What to consider|Key insight|Key feature)[:\*]', bl, re.I):
                    label = re.sub(r'\*\*([^*]+)\*\*.*', r'\1', bl)
                    rest_bl = re.sub(r'\*\*[^*]+\*\*:?\s*', '', bl).strip()
                    body_html += f"""
        <table border=0 cellspacing=0 cellpadding=0 width="100%" style="border-collapse:collapse;margin:10pt 0 0 0;">
          <tr><td style="background:#f5f9f0;padding:10px 14px;border-left:3px solid {accent};">
            <p style="margin:0;font-size:10pt;color:{accent};font-weight:bold;">{label}:</p>
            <p style="margin:4pt 0 0 0;font-size:10pt;color:#1a1a1a;">{_inline(rest_bl)}</p>
          </td></tr>
        </table>"""
                # Social post → subtle grey box
                elif bl.startswith("📱"):
                    social = re.sub(r'^📱\s*(Social post:?)?\s*', '', bl).strip()
                    body_html += f"""
        <table border=0 cellspacing=0 cellpadding=0 width="100%" style="border-collapse:collapse;margin:10pt 0 0 0;">
          <tr><td style="background:#f8f8f8;padding:8px 12px;border-left:3px solid #ccc;">
            <p style="margin:0;font-size:8pt;font-weight:bold;color:#888;letter-spacing:.5pt;">📱 SHARE-READY POST</p>
            <p style="margin:4pt 0 0 0;font-size:10pt;color:#444;">{_inline(social)}</p>
          </td></tr>
        </table>"""
                # Source link
                elif re.match(r'\[Source', bl) or bl.startswith("Source:") or bl.startswith("[Source"):
                    body_html += f'<p style="margin:8pt 0 0 0;font-size:9pt;color:#888;">🔗 {_inline(bl)}</p>'
                else:
                    body_html += f'<p style="margin:0 0 6pt 0;font-size:11pt;color:#1a1a1a;line-height:1.6;">{_inline(bl)}</p>'

            articles.append(f"""
  <tr><td style="padding:16px 28px 8px 28px;">
    <h3 style="margin:0 0 8pt 0;font-size:12pt;font-weight:bold;color:#0f172a;line-height:1.4;">{_inline(headline)}</h3>
    {body_html}
  </td></tr>
  <tr><td style="padding:4px 28px;"><div style="border-top:1px solid #eee;"></div></td></tr>""")

        # ── Section divider (---) → spacer row
        elif s == "---":
            articles.append("""
  <tr><td style="padding:12px 28px;">
    <div style="border-top:2px solid #86BC25;"></div>
  </td></tr>""")

        # ── Regular paragraph (intro note, etc.)
        elif s and not s.startswith(("##", "**", "📱", "#")):
            articles.append(f"""
  <tr><td style="padding:4px 28px;">
    <p style="margin:0 0 6pt 0;font-size:11pt;color:#555;font-style:italic;line-height:1.6;">{_inline(s)}</p>
  </td></tr>""")

        i += 1

    body_rows = "\n".join(articles)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<style>
body {{margin:0;padding:0;background:#F2F2F2;font-family:"Aptos",Calibri,sans-serif;}}
p {{margin:0 0 8pt 0;font-size:11pt;color:#1a1a1a;line-height:1.6;}}
h2 {{margin:14pt 0 6pt 0;font-size:12pt;font-weight:bold;color:#26890D;text-transform:uppercase;letter-spacing:.3pt;}}
a {{color:#26890D;}}
</style>
</head>
<body bgcolor="#F2F2F2">
<table border=0 cellspacing=0 cellpadding=0 width=600 align=center style="background:#F2F2F2;border-collapse:collapse;">

  <!-- BREADCRUMB -->
  <tr>
    <td style="padding:6px 20px 4px 20px;background:#F2F2F2;">
      <p style="margin:0;font-size:7pt;color:#7F7F7F;line-height:1.5;">Southeast Asia &nbsp;|&nbsp; Information Technology &nbsp;|&nbsp; {date_str}</p>
    </td>
  </tr>

  <!-- MASTHEAD -->
  <tr>
    <td style="padding:0 5px 5px 5px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="background:#0f172a;border-collapse:collapse;">
        <tr>
          <td style="padding:22px 28px 24px 28px;">
            <p style="margin:0 0 4pt 0;font-size:20pt;font-weight:bold;color:white;letter-spacing:-0.5pt;">🤖 Daily AI Briefing</p>
            <p style="margin:0 0 6pt 0;font-size:11pt;color:#94a3b8;font-style:italic;">What happened in AI today — curated for SEA IT</p>
            <p style="margin:0;font-size:8pt;color:#475569;border-top:1px solid #334155;padding-top:8pt;">{date_str} &nbsp;&#8226;&nbsp; Sources: HackerNews · RSS · GitHub</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- BODY -->
  <tr>
    <td style="padding:0 5px 5px 5px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="background:white;border-collapse:collapse;">
        <tr><td style="border-top:3px solid #86BC25;padding:0;"></td></tr>
        {body_rows}
        <tr><td style="padding:16px 28px 24px 28px;">
          <p style="margin:0;font-size:9pt;color:#94a3b8;">This briefing is AI-generated from public sources. Verify before acting on any item.</p>
        </td></tr>
      </table>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:16px 20px 20px 20px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="border-collapse:collapse;">
        <tr>
          <td style="padding:16px 20px;background:#1a1a1a;text-align:center;">
            <p style="margin:0 0 4pt 0;font-size:9pt;color:#86BC25;font-weight:bold;letter-spacing:.5pt;text-transform:uppercase;">SEA IT · AI Hub</p>
            <p style="margin:0 0 4pt 0;font-size:8pt;color:#aaa;">This communication is intended solely for Deloitte SEA IT personnel.</p>
            <p style="margin:0;font-size:8pt;color:#666;">Confidential — For Internal Use Only &nbsp;&#8226;&nbsp; &copy; Deloitte {date_str[-4:]}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

</table>
</body></html>"""


# ── GMAIL SMTP ─────────────────────────────────────────────────────────────────

def send_email(subject: str, html: str) -> None:
    """Send via Gmail SMTP (primary — works on macOS).
    Falls back to Outlook COM on Windows when Netskope blocks SMTP."""
    try:
        _send_via_gmail(subject, html)
    except Exception as e:
        log(f"  Gmail SMTP failed ({str(e)[:120]}) — falling back to Outlook COM…")
        _send_via_outlook(subject, html)


def _send_via_outlook(subject: str, html: str) -> None:
    """Send using local Outlook client via PowerShell COM automation."""
    # Write HTML to a temp file to avoid escaping issues in PS args
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_email.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    tmp_ps = tmp.replace("\\", "\\\\")
    ps = f"""
$ol = New-Object -ComObject Outlook.Application
$mail = $ol.CreateItem(0)
$mail.To       = "{RECIPIENT_EMAIL}"
$mail.Subject  = "{subject.replace('"', "'")}"
$mail.HTMLBody = [IO.File]::ReadAllText("{tmp_ps}")
$mail.Send()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=60
    )
    os.remove(tmp)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200])


def _send_via_gmail(subject: str, html: str) -> None:
    """Send via Gmail SMTP (fallback — may be blocked by corporate firewall)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"AI Briefing <{GMAIL_ADDRESS}>"
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())


# ── MAIN ───────────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_seen_cache() -> dict:
    """Load cache of previously seen URLs with timestamps."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}


def save_seen_cache(cache: dict):
    """Save cache of seen URLs."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)


def filter_new_items(items: list[dict], cache: dict, url_key: str = 'url') -> list[dict]:
    """Filter out items seen in the last CACHE_DAYS days."""
    cutoff = (datetime.now() - timedelta(days=CACHE_DAYS)).timestamp()
    new_items = []
    for item in items:
        url = item.get(url_key, '')
        if not url:
            continue
        # Skip if seen recently
        if url in cache and cache[url] > cutoff:
            continue
        new_items.append(item)
        cache[url] = datetime.now().timestamp()
    return new_items


def main():
    _now     = datetime.now()
    today    = _now.strftime("%Y-%m-%d")
    # Build without %-d / %#d so it works on both Windows and Unix
    date_str = f"{_now.strftime('%A, %B')} {_now.day}, {_now.year}"

    # Load deduplication cache
    log("Loading seen-items cache…")
    cache = load_seen_cache()
    initial_cache_size = len(cache)

    log("Fetching HackerNews AI stories…")
    hn = fetch_hackernews()
    log(f"  {len(hn)} stories found")

    log("Fetching RSS feeds…")
    rss_items = []
    for feed in RSS_FEEDS:
        items = fetch_rss(feed)
        rss_items.extend(items)

    log("Fetching GitHub trending AI repos…")
    gh_repos = fetch_github_trending()
    log(f"  {len(gh_repos)} repos found")

    # Reddit disabled - consistently blocked without OAuth
    reddit_posts = []

    log("Fetching Twitter/X AI trends…")
    twitter_trends = fetch_twitter_ai_trends()
    log(f"  {len(twitter_trends)} tweets found")

    log("Fetching ProductHunt AI products…")
    ph_products = fetch_producthunt_ai()
    log(f"  {len(ph_products)} products found")

    log(f"Before dedup: {len(hn)} HN, {len(rss_items)} RSS, {len(gh_repos)} GitHub, {len(reddit_posts)} Reddit, {len(twitter_trends)} Twitter, {len(ph_products)} ProductHunt")

    # Filter out items seen in last 7 days
    log("Filtering out duplicates from last 7 days…")
    hn = filter_new_items(hn, cache, 'url')
    rss_items = filter_new_items(rss_items, cache, 'url')
    gh_repos = filter_new_items(gh_repos, cache, 'url')
    reddit_posts = filter_new_items(reddit_posts, cache, 'url')
    twitter_trends = filter_new_items(twitter_trends, cache, 'url') if twitter_trends else []
    ph_products = filter_new_items(ph_products, cache, 'url') if ph_products else []

    log(f"  After dedup: {len(hn)} HN, {len(rss_items)} RSS, {len(gh_repos)} GitHub, {len(reddit_posts)} Reddit, {len(twitter_trends)} Twitter, {len(ph_products)} ProductHunt")

    # Save updated cache
    save_seen_cache(cache)
    log(f"  Cache updated: {initial_cache_size} → {len(cache)} items")

    # Build raw context string
    lines = ["=== HACKER NEWS TOP AI STORIES ==="]
    for s in hn:
        lines.append(f"- [{s['title']}]({s['url']}) (score:{s['score']})")

    lines.append("\n=== RSS FEED STORIES ===")
    for item in rss_items:
        lines.append(f"- [{item['title']}]({item['url']})")
        if item["desc"]:
            lines.append(f"  {item['desc']}")

    lines.append("\n=== GITHUB TRENDING AI REPOS ===")
    for r in gh_repos:
        lang = r.get('lang', 'Unknown')
        lines.append(f"- [{r['name']}]({r['url']}) ⭐{r['stars']:,} ({lang})")
        if r.get("desc"):
            lines.append(f"  {r['desc']}")

    lines.append("\n=== REDDIT AI COMMUNITY (hot posts) ===")
    for p in reddit_posts:
        lines.append(f"- [r/{p['sub']}] [{p['title']}]({p['url']}) (score:{p['score']}, {p['comments']} comments)")

    if twitter_trends:
        lines.append("\n=== TWITTER/X AI BUZZ (24h) ===")
        for t in twitter_trends:
            lines.append(f"- {t['text']}")

    if ph_products:
        lines.append("\n=== PRODUCT HUNT AI LAUNCHES ===")
        for prod in ph_products:
            lines.append(f"- [{prod['name']}]({prod['url']})")

    raw = "\n".join(lines)
    log(f"Collected {len(hn)} HN + {len(rss_items)} RSS + {len(gh_repos)} GitHub + {len(reddit_posts)} Reddit + {len(twitter_trends)} Twitter + {len(ph_products)} ProductHunt items. Sending to Gemini…")

    try:
        briefing = call_ai(raw, today)

        # Fix: ensure GitHub star counts match the fetched data (Gemini sometimes hallucinates numbers)
        for r in gh_repos:
            repo_name = r['name'].split('/')[-1]  # e.g., "ollama" from "ollama/ollama"
            # Replace any incorrect star count with the actual one
            briefing = re.sub(
                rf'\b{re.escape(repo_name)}\b.*?⭐\d+',
                lambda m: m.group(0).rsplit('⭐', 1)[0] + f"⭐{r['stars']:,}",
                briefing
            )

        log(f"Briefing ready ({len(briefing):,} chars). Splitting into 2 emails…")

        # Split briefing into two emails by section groups
        sections = briefing.split("\n\n---\n\n")

        # Email 1: News & Industry Updates (parts 1-3)
        # - Top 3 Stories, AI News, Governance & Policy, Mindset & Culture, Learning & Best Practices, Prompt Engineering
        email1_content = "\n\n---\n\n".join(sections[0:3])

        # Email 2: Technical Deep Dive (parts 4-6)
        # - Security & Privacy, Ethics, Research, Tools & Resources, Community Conversations
        email2_content = "\n\n---\n\n".join(sections[3:6])

        log(f"Email 1: {len(email1_content):,} chars (News & Learning)")
        log(f"Email 2: {len(email2_content):,} chars (Technical & Community)")

        # Save markdown files for archiving and future LLM use
        archive_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archives")
        os.makedirs(archive_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")

        # Save split versions only
        part1_md_path = os.path.join(archive_dir, f"briefing_{timestamp}_part1_news.md")
        with open(part1_md_path, 'w', encoding='utf-8') as f:
            f.write(f"# AI Briefing Part 1: News & Learning — {date_str}\n\n")
            f.write(email1_content)
        log(f"✓ Saved Part 1: {part1_md_path}")

        part2_md_path = os.path.join(archive_dir, f"briefing_{timestamp}_part2_technical.md")
        with open(part2_md_path, 'w', encoding='utf-8') as f:
            f.write(f"# AI Briefing Part 2: Technical & Community — {date_str}\n\n")
            f.write(email2_content)
        log(f"✓ Saved Part 2: {part2_md_path}")

    except urllib.error.HTTPError as e:
        log(f"ERROR — API HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR — API call failed: {e}")
        sys.exit(1)

    try:
        # Send Email 1: News & Learning
        send_email(
            f"🔥 AI Briefing Part 1: News & Learning — {today}",
            markdown_to_html(email1_content, date_str)
        )
        log(f"✓ Email 1 sent to {RECIPIENT_EMAIL}")

        # Send Email 2: Technical Deep Dive
        send_email(
            f"💻 AI Briefing Part 2: Technical & Community — {today}",
            markdown_to_html(email2_content, date_str)
        )
        log(f"✓ Email 2 sent to {RECIPIENT_EMAIL}")
        log("Done! Both emails delivered.")

    except smtplib.SMTPAuthenticationError:
        log("ERROR — Gmail auth failed. Check GMAIL_APP_PASSWORD in script.")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR — Email failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

