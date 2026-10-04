#!/bin/bash
# Install the compound-engineering plugin in Claude Code cloud sessions,
# which don't install project-enabled plugins on their own.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PLUGIN="compound-engineering@compound-engineering-plugin"

if grep -q "\"$PLUGIN\"" ~/.claude/plugins/installed_plugins.json 2>/dev/null; then
  exit 0
fi

claude plugin marketplace add EveryInc/compound-engineering-plugin
# User scope: avoids rewriting the tracked .claude/settings.json.
claude plugin install "$PLUGIN" --scope user
