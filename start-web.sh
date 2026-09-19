#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

if [[ -z "${HERMES_REMOTE_URL:-}" ]]; then
  # An explicit agent home means the operator wants local mode. Otherwise only
  # auto-select local mode when a complete Hermes virtual environment exists.
  if [[ -z "${HERMES_AGENT_HOME:-}" && ! -x "$HOME/hermes-agent/venv/bin/python" && ! -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]]; then
    export HERMES_REMOTE_URL="http://100.92.91.49:3005"
  fi
fi

if [[ -n "${HERMES_PYTHON:-}" ]]; then
  python_bin="$HERMES_PYTHON"
elif [[ -z "${HERMES_REMOTE_URL:-}" && -n "${HERMES_AGENT_HOME:-}" && -x "$HERMES_AGENT_HOME/venv/bin/python" ]]; then
  python_bin="$HERMES_AGENT_HOME/venv/bin/python"
elif [[ -z "${HERMES_REMOTE_URL:-}" && -x "$HOME/hermes-agent/venv/bin/python" ]]; then
  python_bin="$HOME/hermes-agent/venv/bin/python"
elif [[ -z "${HERMES_REMOTE_URL:-}" && -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]]; then
  python_bin="$HOME/.hermes/hermes-agent/venv/bin/python"
elif [[ -x "$repo_dir/.venv/bin/python" ]]; then
  python_bin="$repo_dir/.venv/bin/python"
else
  python_bin="python3"
fi

if ! "$python_bin" -c 'import fastapi, httpx, uvicorn, websockets, yaml' >/dev/null 2>&1; then
  echo "Hermes Web dependencies are missing for $python_bin" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$python_bin" backend.py "${HERMES_WEB_PORT:-3005}"
