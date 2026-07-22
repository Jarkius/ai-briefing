## How to Remove Proxy Settings from Your Mac

### Check Where Proxy is Set

Open Terminal on your Mac and run:

```bash
# Check environment variables
echo "http_proxy: $http_proxy"
echo "https_proxy: $https_proxy"
echo "HTTP_PROXY: $HTTP_PROXY"
echo "HTTPS_PROXY: $HTTPS_PROXY"

# Check shell config files
grep -i proxy ~/.zshrc ~/.zshenv ~/.bash_profile ~/.bashrc ~/.profile
```

### Option 1: Remove from Shell Config Files

If the grep command above shows proxy settings, edit those files:

```bash
# Open the file that contains proxy settings (e.g., ~/.zshrc)
nano ~/.zshrc

# Look for lines like:
# export http_proxy=http://localhost:3128
# export https_proxy=http://localhost:3128

# Delete or comment them out with #:
# # export http_proxy=http://localhost:3128
# # export https_proxy=http://localhost:3128

# Save and reload
source ~/.zshrc
```

### Option 2: Remove from System Network Settings

1. Open **System Settings**
2. Go to **Network**
3. Select your active connection (Wi-Fi or Ethernet)
4. Click **Details**
5. Go to **Proxies** tab
6. Uncheck all proxy protocols (Web Proxy, Secure Web Proxy, etc.)
7. Click **OK**

### Option 3: Remove from launchd Environment

```bash
# Check if set globally for launchd
launchctl getenv http_proxy
launchctl getenv https_proxy

# If they return values, unset them:
launchctl unsetenv http_proxy
launchctl unsetenv https_proxy
launchctl unsetenv HTTP_PROXY
launchctl unsetenv HTTPS_PROXY
```

### Option 4: Temporary Removal (Current Session Only)

```bash
# Just for testing, unset in current terminal:
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# Test if internet works:
curl -I https://news.ycombinator.com
```

### Verify It's Removed

After removing, verify:

```bash
env | grep -i proxy
# Should return nothing

curl -I https://news.ycombinator.com
# Should return: HTTP/2 200
```

### Most Likely Scenario

Since the proxy variables showed up in your environment but NOT in shell config files, they're probably set in:
- System Settings → Network → Proxies
- Or globally via launchctl

Check both places above.
