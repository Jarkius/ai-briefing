# Daily AI Briefing

Automated daily AI news briefing system that fetches AI news, research, tools, and trends from multiple sources, generates a comprehensive briefing using Gemini, and delivers it via email.

## Features

- **Multi-source aggregation**: HackerNews, RSS feeds, GitHub trending, arXiv papers
- **Smart deduplication**: Tracks seen items for 7 days to avoid repeats
- **AI-powered curation**: Uses Gemini to generate 10-section briefings covering news, learning, security, ethics, and more
- **Dual email delivery**: Splits into "News & Learning" and "Technical & Community" emails
- **Markdown archives**: Saves timestamped briefings for historical tracking
- **Educational focus**: Emphasizes prompt engineering, security best practices, tutorials, and AI awareness

## Architecture

```
Sources → Fetch & Filter → Gemini API → Format → Email + Archive
  ↓           ↓               ↓           ↓           ↓
HN, RSS,   Dedup Cache    10 Sections  HTML/MD    Gmail SMTP
GitHub,    (7 days)       Generated    Split      + Local MD
arXiv
```

## Setup

### Prerequisites

- Python 3.8+
- Gmail account with App Password enabled
- Gemini API access (via maxplus-ai.cc or official endpoint)

### Configuration

Copy `.env.example` to `.env` next to the script and fill in your values:

```bash
cp .env.example .env
```

```dotenv
MAXPLUS_API_KEY=ccsk-your-api-key-here
GEMINI_API_KEY=your-google-gemini-api-key   # optional fallback #1
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password
RECIPIENT_EMAIL=recipient@gmail.com   # optional, defaults to GMAIL_ADDRESS
```

The `.env` file is gitignored — never commit real credentials.

### Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd ai-briefing

# No additional dependencies needed (uses stdlib only)
```

## Usage

### Manual Run

```bash
python3 ai_briefing.py
```

### Scheduled Run (macOS/Linux)

The briefing can be scheduled using:
- **Claude Code scheduled tasks** (recommended): Runs at 10:00 AM daily with hybrid mode
- **Cron**: Add to crontab
- **Launchd**: macOS launch agent

#### Claude Code Scheduled Task

The project includes a scheduled task configured to run daily at 10:00 AM with hybrid mode:

1. **Primary mode**: Runs the Python script for fast, automated delivery
2. **Fallback mode**: If network restrictions block the script, uses web search to compile the briefing

To enable:
1. Open Claude Code
2. Navigate to Scheduled Tasks
3. Find "daily-ai-awareness-briefing"
4. Click "Run now" once to pre-approve permissions

### Scheduled Run (Windows)

Use Windows Task Scheduler with the included wrapper scripts:

1. Copy `.env.example` to `.env` and fill in your credentials (see Configuration above).
2. Right-click `setup_task.bat` → **Run as administrator**. This registers a daily task
   (`\ai\AI Briefing Daily`) that runs `run_briefing.bat`, which in turn calls
   `ai_briefing.py` and logs output to `logs\briefing_YYYY-MM-DD.log`.
3. If the task needs to survive screen lock/logoff, or run on battery, use
   `fix_task_settings.ps1` (run from an elevated PowerShell prompt) to switch the task's
   power/logon settings.

**Email delivery on corporate networks:** if a proxy/DLP agent (e.g. Netskope) blocks
outbound Gmail SMTP, `ai_briefing.py` automatically falls back to sending via a local
Outlook client through PowerShell COM automation. This fallback requires Outlook to be
installed and an interactive desktop session — it will not work if the scheduled task
is set to "Run whether user is logged on or not."

## Briefing Sections

1. 🔥 **Top 3 Stories This Briefing** - Most impactful items
2. 📰 **AI News & Headlines** - Latest announcements and developments
3. 🏛️ **AI Governance & Policy** - Regulations, compliance, enterprise AI
4. 🧠 **AI Mindset & Culture** - How teams are adopting AI
5. 📚 **AI Learning & Best Practices** - Tutorials, workflows, techniques
6. 🎯 **Prompt Engineering Tips** - Effective prompting strategies
7. 🔒 **AI Security & Privacy** - Vulnerabilities, data protection
8. ⚖️ **AI Ethics & Responsible Use** - Fairness, bias, safety
9. 🔬 **AI Research & Emerging Capabilities** - Papers, breakthroughs
10. 💻 **Useful AI Tools & Resources** - GitHub repos, frameworks
11. 💬 **Community Conversations** - HackerNews/Reddit discussions

## Data Sources

### RSS Feeds
- TechCrunch AI
- VentureBeat AI
- Ars Technica
- AI News
- MarkTechPost (ML research & tutorials)
- Google AI Blog
- Hugging Face Blog
- PyTorch Blog
- DeepLearning.AI - The Batch
- HackerNews AI RSS (hnrss.org)
- arXiv Machine Learning papers

### APIs
- HackerNews Firebase API
- GitHub API (trending repos)

### Disabled Sources
- Reddit (blocked by 403 without OAuth)
- Twitter/X (Nitter unreliable)
- ProductHunt (scraping fragile)

## Output

### Email
Two HTML emails sent daily:
1. **Part 1: News & Learning** - Top stories, news, governance, mindset, learning, prompts
2. **Part 2: Technical & Community** - Security, ethics, research, tools, community

### Archives
Markdown files saved to `archives/`:
- `briefing_YYYY-MM-DD_HHMM_part1_news.md` (when script succeeds)
- `briefing_YYYY-MM-DD_HHMM_part2_technical.md` (when script succeeds)
- `briefing_YYYY-MM-DD_HHMM_websearch.md` (fallback mode, single file)

### Cache
`.seen_cache.json` - Tracks URLs from last 7 days to prevent duplicates

## Troubleshooting

### 403 Forbidden Errors
- **Network restrictions in Claude Code scheduled tasks**: The Python script may encounter 403 errors when fetching from external sources (HackerNews, RSS feeds, GitHub) due to sandbox network policies
- **Fallback mode**: The scheduled task automatically switches to web search mode if the script fails, compiling the briefing from search results instead
- **Manual workaround**: Run the script from your local terminal (not Claude Code sandbox) to bypass network restrictions
- Check that RSS feeds haven't changed URLs
- Verify Gemini API key is valid
- Ensure network allows outbound HTTPS

### No New Items After Dedup
- Normal if run multiple times per day
- Cache clears items older than 7 days automatically
- Delete `.seen_cache.json` to force full fetch (testing only)

### Gmail SMTP Errors
- Enable 2FA on Gmail account
- Generate App Password (not regular password)
- Check SMTP isn't blocked by firewall

### Syntax Errors in Python Script
- **Fixed in commit 24c7305**: Removed duplicate section header that caused `SyntaxError: invalid character '⭐'`
- If you encounter similar errors, check for duplicate triple-quoted strings in the `call_ai()` function

## Customization

### Change Email Schedule
Edit the Claude Code scheduled task or modify cron schedule.

### Add/Remove Sources
Edit `RSS_FEEDS` list in `ai_briefing.py`.

### Adjust Deduplication Window
Change `CACHE_MAX_AGE_DAYS` constant (default: 7 days).

### Modify Briefing Sections
Edit the 6 Gemini API calls in `main()` to change section focus.

## License

MIT

## Contributing

Pull requests welcome! Please ensure:
- Code follows existing style
- RSS feeds are tested and working
- Deduplication logic preserved
- Email formatting remains clean

## Author

Created for AI awareness newsletter and daily briefing automation.
# ai-briefing
