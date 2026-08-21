@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py
) else if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" run.py
) else (
  echo [ERROR] 未找到 .venv 或 venv 中的 Python 运行环境。
  exit /b 1
)
