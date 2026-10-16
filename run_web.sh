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

# Reinstall whenever pyproject.toml changed since the last install. The stamp
# is separate from run.sh's because the web client needs the extras too.
if ! python -c "import os,sys; s='.venv/.installed-web'; sys.exit(0 if os.path.exists(s) and os.path.getmtime(s) >= os.path.getmtime('pyproject.toml') else 1)" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -e ".[client,web]"
    touch .venv/.installed-web .venv/.installed
fi

if [ -f ".env.example" ] && [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi

MCP_PID=""
cleanup() {
    trap - EXIT INT TERM
    if [ -n "$MCP_PID" ] && kill -0 "$MCP_PID" 2>/dev/null; then
        echo ""
        echo "Stopping MCP server (PID $MCP_PID)..."
        kill -TERM "$MCP_PID" 2>/dev/null || true
        for _ in {1..10}; do
            if ! kill -0 "$MCP_PID" 2>/dev/null; then
                break
            fi
            sleep 0.2
        done
        if kill -0 "$MCP_PID" 2>/dev/null; then
            kill -KILL "$MCP_PID" 2>/dev/null || true
        fi
        wait "$MCP_PID" 2>/dev/null || true
        echo "Done."
    fi
}
trap cleanup EXIT INT TERM

# Check if MCP server is already running on port 8000
if python -c "import socket; s = socket.create_connection(('127.0.0.1', 8000), timeout=0.5); s.close()" 2>/dev/null; then
    echo "Found existing MCP server on port 8000."
else
    echo ""
    echo "====================================================="
    echo " Starting docker2k8s MCP server (background)"
    echo "====================================================="
    docker2k8s-mcp --transport http &
    MCP_PID=$!

    echo "Waiting for MCP server to start..."
    STARTED=0
    for i in {1..30}; do
        if ! kill -0 "$MCP_PID" 2>/dev/null; then
            echo "Error: MCP server process exited unexpectedly." >&2
            exit 1
        fi
        if python -c "import socket; s = socket.create_connection(('127.0.0.1', 8000), timeout=0.5); s.close()" 2>/dev/null; then
            STARTED=1
            break
        fi
        sleep 0.5
    done

    if [ "$STARTED" -ne 1 ]; then
        echo "Error: Timed out waiting for MCP server to start on port 8000." >&2
        exit 1
    fi
fi

echo ""
echo "====================================================="
echo " Starting docker2k8s Web Client"
echo " Open http://localhost:3000 in your browser"
echo "====================================================="
echo ""
python web_client.py
