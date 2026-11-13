@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "DATA=%APPDATA%\osu-review"

echo.
echo   osu!review - Uninstall
echo   ======================
echo.
echo   Program folder:  %DIR%
echo   Your data:       %DATA%
echo                    (analysed scores, settings, theme, downloaded tools)
echo.
echo   What should be removed?
echo     1 = program only - keep my scores and settings  (use this before installing a new version)
echo     2 = everything - program AND all my data
echo     3 = cancel
echo.
choice /c 123 /n /m "  Choice [1/2/3]: "
if errorlevel 3 goto :cancel
set "ALL=0"
if errorlevel 2 set "ALL=1"

if "%ALL%"=="1" (
  echo.
  echo   This deletes ALL your analysed scores and settings for good.
  choice /c yn /n /m "  Really delete everything? [y/n]: "
  if errorlevel 2 goto :cancel
)

rem ---------------------------------------------------------------- stop osu!review
echo.
echo   Stopping osu!review ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 3 'http://127.0.0.1:47290/api/quit' | Out-Null; Start-Sleep -Seconds 3 } catch {}" ^
  "$dir=$env:DIR.ToLower(); $data=$env:DATA.ToLower(); $ids=@();" ^
  "try { $ids += (Get-NetTCPConnection -LocalPort 47290 -State Listen -ErrorAction Stop).OwningProcess } catch {}" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|pyw|py|node|ffmpeg|danser.*)\.exe$' -and ((''+$_.CommandLine).ToLower().Contains($dir) -or (''+$_.CommandLine).ToLower().Contains($data) -or (''+$_.CommandLine).ToLower().Contains($data) -or (''+$_.ExecutablePath).ToLower().StartsWith($dir) -or (''+$_.ExecutablePath).ToLower().StartsWith($data)) } | ForEach-Object { $ids += $_.ProcessId };" ^
  "$ids | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
timeout /t 2 >nul

rem ---------------------------------------------------------------- shortcuts
powershell -NoProfile -Command ^
  "foreach($f in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Startup'))){ $l=Join-Path $f 'osu!review.lnk'; if(Test-Path -LiteralPath $l){ Remove-Item -LiteralPath $l -Force } }"
echo   Desktop shortcut and autostart removed.

rem ---------------------------------------------------------------- data
if "%ALL%"=="1" (
  if exist "%DATA%\" rmdir /s /q "%DATA%"
  if exist "%DATA%\" (echo   Some data could not be deleted - is osu!review still open?) else (echo   Your data was deleted.)
) else (
  echo   Your scores and settings stay in %DATA%
  echo   - the next version picks them up automatically.
)

rem ---------------------------------------------------------------- program files
rem Only osu!review's own files are deleted (never anything else that sits in this folder).
cd /d "%TEMP%"
for %%F in (app.py window.py version.json Start.bat Install.bat README.txt Starten.bat Installieren.bat LIESMICH.txt) do if exist "%DIR%\%%F" del /f /q "%DIR%\%%F"
for %%D in (core web python tests diagnostics data __pycache__) do if exist "%DIR%\%%D\" rmdir /s /q "%DIR%\%%D"

echo.
echo   osu!review was uninstalled.
echo   Your rendered videos (Videos\osu!review) were not touched.
if "%ALL%"=="0" echo   To install the new version: unzip it and run Install.bat.
echo.
pause
rem this file deletes itself last, then the folder if it is empty now
start "" /min cmd /c "timeout /t 2 >nul & del /f /q "%DIR%\Uninstall.bat" & rmdir "%DIR%" 2>nul"
exit /b 0

:cancel
echo.
echo   Cancelled - nothing was changed.
echo.
pause
exit /b 0
