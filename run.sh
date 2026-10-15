#!/bin/bash

# Database in the add-on's own config folder (/addon_configs/<slug> on the host) so it survives updates
export DB_PATH="/config/meal_planner.db"

# Claude sign-in (made on the Plan tab) is kept here so it survives updates.
export CLAUDE_CONFIG_DIR="/data/claude"
mkdir -p "$CLAUDE_CONFIG_DIR"

# Optional fallback: token from the Configuration tab (made with `claude setup-token`).
# Strip all whitespace: copying a wrapped token out of a terminal adds line breaks.
token="$(python3 -c 'import json; print("".join((json.load(open("/data/options.json")).get("claude_code_oauth_token") or "").split()))' 2>/dev/null)"
if [ -n "$token" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$token"
fi

python3 db.py

# Web UI and API: Home Assistant ingress, plus /meals/* for Claude Terminal on the internal network
exec uvicorn api:app --host 0.0.0.0 --port 8502