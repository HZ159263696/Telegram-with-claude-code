#!/bin/bash
set -e

echo "=== Installing system packages ==="
sudo apt-get update -qq
sudo apt-get install -y tmux jq curl python3 ca-certificates gnupg

echo "=== Installing Node.js 22 ==="
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - 2>&1 | tail -5
sudo apt-get install -y nodejs

echo "=== Installing Claude Code ==="
sudo npm install -g @anthropic-ai/claude-code

echo "=== All done ==="
node --version
claude --version 2>/dev/null || echo "claude installed"
