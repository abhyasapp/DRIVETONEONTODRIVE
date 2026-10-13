@echo off
setlocal
cd /d "%~dp0.."

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

set PY=%~dp0..\.venv\Scripts\python.exe
if not exist "%PY%" set PY=%~dp0..\venv\Scripts\python.exe
if not exist "%PY%" (
    echo ERROR: no venv found under .venv\ or venv\.
    echo Create one with:  python -m venv .venv
    exit /b 1
)

"%PY%" -u -m pipeline.run_pipeline
