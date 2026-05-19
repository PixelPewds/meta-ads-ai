#!/usr/bin/env bash
# One-command bootstrap (macOS / Linux).
# First run takes 1-3 minutes. Subsequent runs are instant.
set -e

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — open it and set ANTHROPIC_API_KEY."
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.installed ]; then
  echo
  echo "=== First-time setup ==="
  echo "Installing dependencies. This takes 1-3 minutes the first time."
  echo
  python -m pip install --upgrade pip
  python -m pip install --prefer-binary -r requirements.txt
  touch .venv/.installed
  echo
  echo "=== Setup complete ==="
else
  python -m pip install --prefer-binary -q -r requirements.txt
fi

echo
echo "Starting Meta Ads AI on http://localhost:8000"
exec uvicorn backend.main:app --reload --port 8000
