@echo off
REM One-command bootstrap (Windows).
REM First run takes 1-3 minutes (pandas + Anthropic SDK download).
REM Subsequent runs start in a few seconds.

cd /d "%~dp0"

IF NOT EXIST .env (
  copy .env.example .env >NUL
  echo Created .env from .env.example. Open it and set ANTHROPIC_API_KEY.
)

IF NOT EXIST .venv (
  echo Creating virtual environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Marker file so we skip the slow install on subsequent runs.
IF NOT EXIST .venv\.installed (
  echo.
  echo === First-time setup ===
  echo Installing dependencies. This takes 1-3 minutes the first time.
  echo Largest packages: pandas (~30 MB), anthropic SDK, reportlab.
  echo You will see progress lines below — please wait, do not close this window.
  echo.
  python -m pip install --upgrade pip
  python -m pip install --prefer-binary -r requirements.txt
  IF ERRORLEVEL 1 (
    echo.
    echo *** pip install failed. Check the error above. ***
    pause
    exit /b 1
  )
  echo. > .venv\.installed
  echo.
  echo === Setup complete ===
) ELSE (
  REM Quick incremental check (fast — only installs missing or out-of-date packages).
  python -m pip install --prefer-binary -q -r requirements.txt
)

echo.
echo Starting Meta Ads AI on http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn backend.main:app --reload --port 8000
