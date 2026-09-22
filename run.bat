@echo off
REM 一键启动 myharness
REM 用法：双击本文件，或在终端输入 run

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] 没有找到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

".venv\Scripts\python.exe" -m myharness %*
