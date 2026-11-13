@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo.
echo   osu!review - build Setup.exe
echo   ============================
echo   First run downloads Python, pywebview and Inno Setup into build\ (about 40 MB).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-setup.ps1"
if errorlevel 1 (
  echo.
  echo   Something went wrong - see the message above.
  pause
  exit /b 1
)
echo.
explorer "%cd%\dist"
pause
