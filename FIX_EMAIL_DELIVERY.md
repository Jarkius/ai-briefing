## Fix AI Briefing Email Delivery

### Problem Summary
- The briefing script runs at 6am via macOS launchd
- Network requests fail due to incorrect proxy configuration
- Proxy variables point to localhost:3128 but nothing is listening there

### Solution: Update launchd Configuration

Open Terminal on your Mac and run these commands:

```bash
# 1. Unload the old configuration
launchctl unload ~/Library/LaunchAgents/com.user.ai-briefing.plist

# 2. Copy the updated plist
cp /Users/jarkius/workspace/dev/ai-briefing/com.user.ai-briefing.plist ~/Library/LaunchAgents/

# 3. Reload with new configuration
launchctl load ~/Library/LaunchAgents/com.user.ai-briefing.plist

# 4. Test the script manually
cd /Users/jarkius/workspace/dev/ai-briefing
python3 ai_briefing.py
```

### What Changed
The updated plist now explicitly clears proxy environment variables so the script can access the internet directly.

### Testing
After running the commands above, the manual test should:
- Fetch news from HackerNews, RSS feeds, and GitHub
- Generate a briefing with Gemini API
- Send two HTML emails to juckrit@gmail.com
- Save markdown archive

Check the logs:
```bash
tail -f /Users/jarkius/workspace/dev/ai-briefing/briefing.log
```

### Next Scheduled Run
Tomorrow at 6:00 AM, the script will run automatically with the fixed configuration.

### Alternative: Remove Proxy Variables from Shell
If you want to remove these proxy variables from your shell permanently:

```bash
# Check your shell config files for proxy settings
grep -i proxy ~/.zshrc ~/.zshenv ~/.bash_profile ~/.bashrc

# Edit the file and remove or comment out proxy lines
```
