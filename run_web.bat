@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Reinstall whenever pyproject.toml changed since the last install. The stamp
REM is separate from run.bat's because the web client needs the extras too.
python -c "import os,sys; s='.venv/.installed-web'; sys.exit(0 if os.path.exists(s) and os.path.getmtime(s) >= os.path.getmtime('pyproject.toml') else 1)" || (
    echo Installing dependencies...
    pip install -e ".[client,web]" && type nul > .venv\.installed-web && type nul > .venv\.installed
)

if exist ".env.example" if not exist ".env" (
    copy .env.example .env
    echo Created .env from .env.example
)

echo.
echo =====================================================
echo  Starting docker2k8s MCP server (background window)
echo =====================================================
start "docker2k8s MCP Server" cmd /c "call .venv\Scripts\activate.bat && docker2k8s-mcp --transport http"

echo Waiting for MCP server to start...
timeout /t 4 /nobreak >nul

echo.
echo =====================================================
echo  Starting docker2k8s Web Client
echo  Open http://localhost:3000 in your browser
echo =====================================================
echo.
python web_client.py

echo.
echo Stopping MCP server...
taskkill /FI "WINDOWTITLE eq docker2k8s MCP Server" /T /F >nul 2>&1
echo Done.
