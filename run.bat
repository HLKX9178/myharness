@echo off
REM One-click launcher for myharness.
REM Usage: double-click this file, or type "run" in a terminal.
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat with the system
REM OEM codepage, so non-ASCII characters here break parsing.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv not found. Please run:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

REM A venv can exist while still empty -- "python -m venv" installs nothing.
REM Without this probe the user gets a bare ModuleNotFoundError, which never
REM says which command fixes it.
REM The probe list mirrors the runtime deps declared in requirements.txt.
".venv\Scripts\python.exe" -c "import dotenv, openai, anthropic, croniter" >nul 2>&1
if errorlevel 1 (
    echo [!] Dependencies are missing from .venv. Please run:
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

".venv\Scripts\python.exe" -m myharness %*
