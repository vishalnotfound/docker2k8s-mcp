#!/usr/bin/env bash
set -e

# Navigate to the project root directory
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find Python 3
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "Error: Python 3 is required but neither python3 nor python was found." >&2
    exit 1
fi

# Set up or activate virtual environment
if [ ! -f ".venv/bin/activate" ]; then
    echo "Creating virtual environment..."
    rm -rf .venv
    "$PYTHON_CMD" -m venv .venv
fi
source .venv/bin/activate

# Reinstall whenever pyproject.toml changed since the last install, so new
# dependencies and entry points are picked up.
if ! python -c "import os,sys; s='.venv/.installed'; sys.exit(0 if os.path.exists(s) and os.path.getmtime(s) >= os.path.getmtime('pyproject.toml') else 1)" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -e .
    touch .venv/.installed
fi

if [ -f ".env.example" ] && [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi

if [ $# -eq 0 ]; then
    echo ""
    echo "======================================================="
    echo "Starting docker2k8s MCP server over HTTP on port 8000"
    echo "======================================================="
    echo ""
    echo "Clients can connect to http://127.0.0.1:8000/mcp"
    echo "Press Ctrl+C to stop."
    echo ""
    exec docker2k8s-mcp --transport http
else
    exec docker2k8s-mcp "$@"
fi
