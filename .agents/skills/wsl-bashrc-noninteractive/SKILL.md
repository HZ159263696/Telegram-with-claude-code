---
name: wsl-bashrc-noninteractive
description: |
  Fix for environment variables not loading in WSL2 when sourcing ~/.bashrc from
  non-interactive shells. Use when: (1) `source ~/.bashrc` silently does nothing in
  a bash -c script, (2) exported variables are present in ~/.bashrc but `echo $VAR`
  returns empty, (3) running `wsl -d Ubuntu -- bash -c 'source ~/.bashrc && ...'`
  produces empty variables. Root cause: Ubuntu's default ~/.bashrc has an early
  `return` for non-interactive shells. Solution: store vars in /etc/claude_env.sh
  or /etc/environment instead.
author: Codex
version: 1.0.0
date: 2026-03-16
---

# WSL .bashrc Non-Interactive Early Exit

## Problem
Environment variables exported in `~/.bashrc` are not available when running
commands via `wsl -d Ubuntu -- bash -c '...'` or other non-interactive bash
invocations, even after explicitly running `source ~/.bashrc`.

## Context / Trigger Conditions
- `wsl -d Ubuntu -- bash -c 'source ~/.bashrc && echo $MY_VAR'` returns empty
- Variable IS present in `~/.bashrc` (confirmed by `cat`)
- Works fine in an interactive `wsl -d Ubuntu` terminal session
- Happens on Ubuntu 20.04, 22.04, 24.04 (default .bashrc)

## Root Cause
Ubuntu's default `~/.bashrc` starts with:
```bash
# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac
```
This `return` exits the script immediately for non-interactive shells,
so any `export` statements below it are never reached.

## Solution

**Option A (recommended): dedicated env file**
```bash
# Create /etc/claude_env.sh (or ~/.env)
echo 'export MY_VAR=value' | sudo tee /etc/claude_env.sh
sudo chmod 644 /etc/claude_env.sh

# Source it explicitly (works in any shell type)
source /etc/claude_env.sh && echo $MY_VAR
```

**Option B: /etc/environment**
```bash
# /etc/environment uses KEY=VALUE format (no export, no shell expansion)
echo 'MY_VAR=value' | sudo tee -a /etc/environment

# Load it with:
set -a; source /etc/environment; set +a
```

**Option C: add export BEFORE the early return in ~/.bashrc**
```bash
# Insert at the very top of ~/.bashrc, before the case statement
export MY_VAR=value
```

## Verification
```bash
wsl -d Ubuntu -- bash -c 'source /etc/claude_env.sh && echo $MY_VAR'
# Should print the value, not empty
```

## Notes
- `~/.profile` has the same issue when sourced non-interactively
- PowerShell's `$` interpolation also interferes: use Python scripts or
  dedicated shell scripts to avoid double-escaping issues
- `/etc/environment` is KEY=VALUE only — no `export`, no `$VAR` expansion,
  no command substitution
