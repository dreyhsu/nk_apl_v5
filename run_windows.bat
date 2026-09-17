@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo VisionEdge - Windows launcher
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: python.exe was not found in PATH.
  echo Activate your Conda environment first.
  pause
  exit /b 1
)

python --version
if errorlevel 1 (
  echo ERROR: Python cannot start.
  pause
  exit /b 1
)

echo.
echo Installing/checking dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: Dependency installation failed.
  pause
  exit /b 1
)

echo.
echo Starting VisionEdge. Default config: HTTPS port 8080.
python -u visionedge_server.py
set RC=%ERRORLEVEL%

echo.
echo VisionEdge exited with code %RC%.
pause
exit /b %RC%
