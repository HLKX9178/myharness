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

".venv\Scripts\python.exe" -m myharness %*
