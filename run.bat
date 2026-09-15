@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -e .
    if exist ".env.example" if not exist ".env" (
        copy .env.example .env
        echo Created .env from .env.example
    )
) else (
    call .venv\Scripts\activate.bat
)

if "%~1"=="" (
    echo.
    echo =======================================================
    echo Starting docker2k8s MCP server over HTTP on port 8000
    echo =======================================================
    echo.
    echo Clients can connect to http://127.0.0.1:8000/mcp
    echo Press Ctrl+C to stop.
    echo.
    docker2k8s-mcp --transport http
) else (
    docker2k8s-mcp %*
)
