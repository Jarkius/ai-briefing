#!/bin/bash
# Test script to verify briefing works without proxy

echo "=== AI Briefing Test Run ==="
echo "Date: $(date)"
echo ""

# Clear proxy variables
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

# Show environment
echo "Proxy variables cleared:"
env | grep -i proxy || echo "(none found - good!)"
echo ""

# Test internet connectivity
echo "Testing internet access..."
if curl -s -I https://news.ycombinator.com | head -1; then
    echo "✓ Internet access OK"
else
    echo "✗ Internet access FAILED"
    exit 1
fi
echo ""

# Run the briefing script
echo "Running briefing script..."
cd /Users/jarkius/workspace/dev/ai-briefing
python3 ai_briefing.py

echo ""
echo "=== Test Complete ==="
