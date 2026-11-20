@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   osu!review - Setup
echo   ==================
echo.
if exist "python\pythonw.exe" goto :havepy
where pythonw >nul 2>nul && goto :havepy
where pyw >nul 2>nul && goto :havepy

echo   Python is not installed - osu!review downloads its own small
echo   Python into this folder (about 11 MB, nothing on your system
echo   is changed)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12';" ^
  "$u='https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip';" ^
  "Invoke-WebRequest $u -OutFile 'python-embed.zip';" ^
  "if ((Get-FileHash 'python-embed.zip' -Algorithm MD5).Hash -ne 'FE8EF205F2E9C3BA44D0CF9954E1ABD3') { Remove-Item 'python-embed.zip'; throw 'download does not match python.org checksum' };" ^
  "Expand-Archive 'python-embed.zip' -DestinationPath 'python' -Force;" ^
  "Remove-Item 'python-embed.zip';" ^
  "Get-ChildItem 'python\*._pth' | ForEach-Object { Add-Content $_.FullName '..' }"
if not exist "python\pythonw.exe" (
  echo.
  echo   The download did not work. Please install Python 3 yourself:
  echo   https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo   and then run this file again.
  echo.
  pause
  exit /b 1
)
:havepy
echo   Python is ready - no other packages are needed.
echo.

set "TARGET=%~dp0Start.bat"
set "ICON=%~dp0web\icon.ico"

set "WORKDIR=%~dp0"
rem paths go in through environment variables, so folder names with ' or other odd characters can't break the command
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\osu!review.lnk');" ^
  "$s.TargetPath=$env:TARGET; $s.WorkingDirectory=$env:WORKDIR; $s.IconLocation=$env:ICON; $s.WindowStyle=7; $s.Save()"
echo   Desktop shortcut "osu!review" created.
echo.

echo   Start osu!review automatically with Windows?
echo   (y = yes, n = no - no answer within 10 seconds: no)
choice /c yn /t 10 /d n /n /m "  Choice [y/n]: "
if errorlevel 2 goto :noauto
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\osu!review.lnk');" ^
  "$s.TargetPath=$env:TARGET; $s.WorkingDirectory=$env:WORKDIR; $s.IconLocation=$env:ICON; $s.WindowStyle=7; $s.Save()"
echo   Autostart set up.
:noauto
echo.
echo   Done! osu!review is starting now.
start "" "%TARGET%"
timeout /t 3 >nul
