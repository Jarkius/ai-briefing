## AI Briefing Setup - Final Configuration

### Current Status
✅ Claude scheduled task disabled (was running in sandbox with no internet)
✅ Updated launchd plist with proxy variables cleared
✅ Test script created

### What You Need to Do on Your Mac Terminal

#### Step 1: Update launchd Configuration

```bash
# Copy the updated plist to LaunchAgents
cp /Users/jarkius/workspace/dev/ai-briefing/com.user.ai-briefing.plist ~/Library/LaunchAgents/

# Unload old configuration
launchctl unload ~/Library/LaunchAgents/com.user.ai-briefing.plist 2>/dev/null

# Load new configuration
launchctl load ~/Library/LaunchAgents/com.user.ai-briefing.plist

# Verify it's loaded
launchctl list | grep ai-briefing
```

#### Step 2: Test the Script Manually

```bash
# Run the test script
cd /Users/jarkius/workspace/dev/ai-briefing
bash test_briefing.sh
```

This will:
- Clear proxy variables
- Test internet connectivity
- Run the briefing script
- Fetch news from HackerNews, RSS, GitHub
- Generate briefing with Gemini
- Send emails to juckrit@gmail.com
- Save markdown archive

#### Step 3: Check the Results

```bash
# Check the log output
tail -50 /Users/jarkius/workspace/dev/ai-briefing/briefing.log

# Check your email for two messages:
# - Part 1: News & Learning
# - Part 2: Technical & Community

# Check the archive was created
ls -lh /Users/jarkius/workspace/dev/ai-briefing/archives/ | tail -5
```

### Troubleshooting

**If you get DNS errors:**
```bash
# Your proxy might still be set. Check:
env | grep -i proxy

# If you see proxy variables, find where they're set:
grep -r "export.*proxy" ~/.zshrc ~/.zshenv ~/.zprofile ~/.profile

# Comment out those lines or remove them
```

**If emails don't send:**
```bash
# Test Gmail connection manually
python3 -c "import smtplib; smtplib.SMTP_SSL('smtp.gmail.com', 465).quit(); print('Gmail SMTP OK')"
```

### Next Scheduled Run

**Time:** Every day at 6:00 AM Bangkok time (Asia/Bangkok)

The launchd service will run automatically. No need for Claude scheduled task anymore.

### Summary of Changes

1. ❌ **Old setup:** Claude scheduled task → runs in sandbox → no internet → fails
2. ✅ **New setup:** macOS launchd → runs on your Mac → has internet → works

The briefing will now:
- Run directly on your Mac at 6am
- Have full internet access (without needing your proxy)
- Fetch fresh AI news daily
- Email you automatically
- Save archives for reference
