@echo off
:: Ralph Wiggum Loop Runner — Windows Launcher
:: Usage: ralph-loop.bat "Process Needs_Action" --max-iterations 10
::        ralph-loop.bat --dry-run
::        ralph-loop.bat --max-iterations 5 --quiet
::
:: Setup: Add this folder to PATH or run from project root.
:: Requires: ANTHROPIC_API_KEY environment variable set.

setlocal

:: Resolve the directory of this .bat file (project root)
set "PROJECT_ROOT=%~dp0"

:: Check Python is available
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.10+.
    exit /b 1
)

:: Check anthropic SDK
python -c "import anthropic" >nul 2>nul
if errorlevel 1 (
    echo ERROR: anthropic SDK not installed.
    echo Run: pip install anthropic
    exit /b 1
)

:: Check ANTHROPIC_API_KEY
if "%ANTHROPIC_API_KEY%"=="" (
    echo ERROR: ANTHROPIC_API_KEY is not set.
    echo Set it with: setx ANTHROPIC_API_KEY "sk-ant-..."
    exit /b 1
)

:: Run the loop
python "%PROJECT_ROOT%tools\ralph_loop_runner.py" %*

endlocal
