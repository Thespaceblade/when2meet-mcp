#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
npx -y @anthropic-ai/mcpb@latest validate manifest.json
npx -y @anthropic-ai/mcpb@latest pack . when2meet-mcp.mcpb
