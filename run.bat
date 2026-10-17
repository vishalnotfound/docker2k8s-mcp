@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Reinstall whenever pyproject.toml changed since the last install, so new
REM dependencies and entry points are picked up.
python -c "import os,sys; s='.venv/.installed'; sys.exit(0 if os.path.exists(s) and os.path.getmtime(s) >= os.path.getmtime('pyproject.toml') else 1)" || (
    echo Installing dependencies...
    pip install -e . && type nul > .venv\.installed
)

if exist ".env.example" if not exist ".env" (
    copy .env.example .env
    echo Created .env from .env.example
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
