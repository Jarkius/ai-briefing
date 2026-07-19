#!/usr/bin/env python3
"""
Daily AI Briefing
1. Fetches live AI news from HackerNews API + RSS feeds
2. Summarizes with Claude (claude-sonnet-5) via maxplus API
3. Emails result via Gmail SMTP to juckrit@gmail.com
Run:       python3 ai_briefing.py
Scheduled: launchd via com.user.ai-briefing.plist
"""

import json, re, sys, smtplib, os
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
MAXPLUS_API_KEY    = "ccsk-REDACTED"
MAXPLUS_MODEL      = "gemini-3.5-flash"           # Gemini 3.5 Flash — latest
GMAIL_ADDRESS      = "juckrit@gmail.com"
GMAIL_APP_PASSWORD = "REDACTED-APP-PASSWORD"
RECIPIENT_EMAIL    = "juckrit@gmail.com"

# Deduplication cache (stores URLs seen in last 7 days)
CACHE_FILE = os.path.expanduser("~/workspace/dev/ai-briefing/.seen_cache.json")
CACHE_DAYS = 7  # Only show items not seen in last 7 days

RSS_FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.artificialintelligence-news.com/feed/",           # AI News
    "https://www.marktechpost.com/feed/",                           # ML/AI research & tutorials
    "https://blog.google/technology/ai/rss/",                       # Google AI blog
    "https://huggingface.co/blog/feed.xml",                         # Hugging Face blog
    "https://pytorch.org/feed.xml",                                 # PyTorch official blog
    "https://www.deeplearning.ai/the-batch/rss/",                   # DeepLearning.AI - The Batch (correct URL)
    "https://hnrss.org/newest?q=AI+OR+LLM+OR+Machine+Learning",   # HackerNews AI RSS (hnrss.org mirror)
    "http://export.arxiv.org/rss/cs.LG",                            # arXiv Machine Learning papers
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
    try:
        # Use proper User-Agent to avoid 403s
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "python:ai-briefing:v1.0 (AI newsletter aggregator)"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            raw_xml = r.read()

        # Try parsing with ET - catches most malformed XML
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as e:
            log(f"  RSS {url.split('/')[2]} failed: XML parse error - {e}")
            return []

    except urllib.error.HTTPError as e:
        log(f"  RSS {url.split('/')[2]} failed: HTTP {e.code}")
        return []
    except Exception as e:
        log(f"  RSS {url.split('/')[2]} failed: {e}")
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

def _grok_call(system: str, user: str) -> str:
    """Single Gemini API call, max 4000 tokens."""
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

    # DEBUG: write sanitized payload
    with open("/Users/jarkius/workspace/dev/ai-briefing/debug_sanitized.txt", "w") as f:
        f.write(sanitized)

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
                  r'<a href="\2" style="color:#3b82f6">\1</a>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*',     r'<em>\1</em>',         text)
    return text


def markdown_to_html(text: str, date_str: str) -> str:
    """Convert markdown to newsletter-ready HTML with icons, hashtags, and social-ready formatting."""
    lines, parts = text.split("\n"), []
    i = 0

    while i < len(lines):
        s = lines[i].strip()

        # Section headers with emoji icons
        if s.startswith("## "):
            parts.append(
                f'<div style="margin:32px 0 20px;padding:16px;background:#f8fafc;border-left:4px solid #3b82f6;border-radius:8px">'
                f'<h2 style="color:#1e293b;margin:0;font-size:20px;font-weight:600">{_inline(s[3:])}</h2>'
                f'</div>'
            )

        # Story/item blocks with visual structure
        elif s.startswith("**") and "**" in s[2:]:
            # Extract headline
            end_idx = s.index("**", 2)
            headline = s[2:end_idx]
            rest = s[end_idx+2:].strip()

            # Build story card
            card = f'<div style="margin:24px 0;padding:20px;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08)">'
            card += f'<h3 style="margin:0 0 12px;color:#0f172a;font-size:18px;font-weight:600;line-height:1.4">{_inline(headline)}</h3>'

            # Body paragraphs
            body_lines = [rest] if rest else []
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if next_line.startswith(("**", "## ", "📱", "---", "")) or next_line.startswith("#"):
                    break
                body_lines.append(next_line)
                i += 1
            i -= 1

            # Format body with spacing
            for body_line in body_lines:
                if body_line.startswith("**Key"):
                    card += f'<div style="margin:12px 0 8px;padding:10px;background:#fef3c7;border-left:3px solid #f59e0b;border-radius:4px"><strong style="color:#92400e">💡 {_inline(body_line[2:])}</strong></div>'
                elif body_line.startswith("**Why"):
                    card += f'<div style="margin:12px 0 8px;padding:10px;background:#dbeafe;border-left:3px solid #3b82f6;border-radius:4px"><strong style="color:#1e40af">🎯 {_inline(body_line[2:])}</strong></div>'
                elif body_line.startswith("📱"):
                    # Social post callout
                    social = body_line.replace("📱 Social post:", "").replace("📱", "").strip()
                    card += f'<div style="margin:16px 0 8px;padding:12px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px">'
                    card += f'<div style="color:#15803d;font-size:13px;font-weight:600;margin-bottom:6px">📱 READY TO SHARE</div>'
                    card += f'<div style="color:#166534;font-size:14px;line-height:1.5">{_inline(social)}</div>'
                    card += '</div>'
                elif body_line.startswith("[Source]") or body_line.startswith("Source:"):
                    # Source link
                    card += f'<div style="margin:12px 0 0;padding-top:12px;border-top:1px solid #e2e8f0"><span style="font-size:12px;color:#64748b">🔗 {_inline(body_line)}</span></div>'
                elif "#" in body_line and body_line.startswith("#"):
                    # Hashtags
                    card += f'<div style="margin:8px 0 0"><span style="font-size:13px;color:#3b82f6">{_inline(body_line)}</span></div>'
                else:
                    card += f'<p style="margin:8px 0;color:#334155;line-height:1.7">{_inline(body_line)}</p>'

            card += '</div>'
            parts.append(card)

        # Section dividers
        elif s == "---":
            parts.append('<hr style="border:none;border-top:2px solid #e2e8f0;margin:32px 0">')

        # Regular paragraphs
        elif s and not s.startswith(("##", "**", "📱", "#")):
            parts.append(f'<p style="margin:12px 0;color:#475569;line-height:1.7">{_inline(s)}</p>')

        i += 1

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
             max-width:680px;margin:0 auto;padding:20px;background:#f8fafc">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#0f172a,#1e293b);padding:28px 32px;border-radius:16px;margin-bottom:32px;box-shadow:0 4px 6px rgba(0,0,0,0.1)">
    <h1 style="color:#fff;margin:0;font-size:26px;font-weight:700;letter-spacing:-0.5px">🤖 Daily AI Briefing</h1>
    <p style="color:#cbd5e1;margin:8px 0 0;font-size:14px">{date_str}</p>
  </div>

  <!-- Content -->
  <div style="background:#ffffff;padding:32px;border-radius:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
    {body}
  </div>

  <!-- Footer -->
  <div style="margin-top:32px;padding:20px;text-align:center;color:#94a3af;font-size:13px;background:#ffffff;border-radius:12px">
    <p style="margin:0 0 8px"><strong>Sources:</strong> HackerNews · RSS Feeds · GitHub · Gemini AI</p>
    <p style="margin:0">Curated by AI · Delivered with ❤️</p>
  </div>

</body></html>"""


# ── GMAIL SMTP ─────────────────────────────────────────────────────────────────

def send_email(subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"AI Briefing <{GMAIL_ADDRESS}>"
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
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
    today    = datetime.now().strftime("%Y-%m-%d")
    date_str = datetime.now().strftime("%A, %B %-d, %Y")

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
        lines.append(f"- {s['title']} (score:{s['score']}) — {s['url']}")

    lines.append("\n=== RSS FEED STORIES ===")
    for item in rss_items:
        lines.append(f"- {item['title']} — {item['url']}")
        if item["desc"]:
            lines.append(f"  {item['desc']}")

    lines.append("\n=== GITHUB TRENDING AI REPOS ===")
    for r in gh_repos:
        lang = r.get('lang', 'Unknown')
        lines.append(f"- {r['name']} ⭐{r['stars']:,} ({lang}) — {r['url']}")
        if r.get("desc"):
            lines.append(f"  {r['desc']}")

    lines.append("\n=== REDDIT AI COMMUNITY (hot posts) ===")
    for p in reddit_posts:
        lines.append(f"- [r/{p['sub']}] {p['title']} (score:{p['score']}, {p['comments']} comments) — {p['url']}")

    if twitter_trends:
        lines.append("\n=== TWITTER/X AI BUZZ (24h) ===")
        for t in twitter_trends:
            lines.append(f"- {t['text']}")

    if ph_products:
        lines.append("\n=== PRODUCT HUNT AI LAUNCHES ===")
        for prod in ph_products:
            lines.append(f"- {prod['name']} — {prod['url']}")

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
        archive_dir = os.path.expanduser("~/workspace/dev/ai-briefing/archives")
        os.makedirs(archive_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")

        # Save split versions only
        part1_md_path = os.path.join(archive_dir, f"briefing_{timestamp}_part1_news.md")
        with open(part1_md_path, 'w') as f:
            f.write(f"# AI Briefing Part 1: News & Learning — {date_str}\n\n")
            f.write(email1_content)
        log(f"✓ Saved Part 1: {part1_md_path}")

        part2_md_path = os.path.join(archive_dir, f"briefing_{timestamp}_part2_technical.md")
        with open(part2_md_path, 'w') as f:
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

