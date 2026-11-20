# Builds dist\osu-review-Setup-<version>.exe from this folder.
# Used by installer\Build-Setup.bat (on your PC) and by the GitHub workflow.
# Everything it downloads (Python, pywebview, Inno Setup) stays inside build\ - nothing is installed on the system.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force build | Out-Null
try { Start-Transcript -Path "build\build-log.txt" -Force | Out-Null } catch {}

$ver = (Get-Content version.json -Raw | ConvertFrom-Json).version
$num = ($ver -split '-')[0]
Write-Host "osu!review $ver - building the setup"

# 1. program files
$app = "build\osu-review"
if (Test-Path $app) { Remove-Item $app -Recurse -Force }
New-Item -ItemType Directory -Force $app | Out-Null
Copy-Item app.py, window.py, version.json, README.txt $app
Copy-Item core, web -Destination $app -Recurse
Get-ChildItem $app -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Get-ChildItem $app -Recurse -File -Filter gh_bundle.json | Remove-Item -Force
Write-Host "  [1/4] program files copied"

# 2. own Python 3.12 with pywebview (kept in build\cache so a rebuild doesn't download again)
$cache = "build\cache"
New-Item -ItemType Directory -Force $cache | Out-Null
$py = "$cache\python"
if (-not (Test-Path "$py\Lib\site-packages\webview")) {
  if (Test-Path $py) { Remove-Item $py -Recurse -Force }
  Invoke-WebRequest https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip -OutFile "$cache\py.zip"
  if ((Get-FileHash "$cache\py.zip" -Algorithm MD5).Hash -ne 'FE8EF205F2E9C3BA44D0CF9954E1ABD3') { throw "Python download does not match python.org's checksum" }
  Expand-Archive "$cache\py.zip" -DestinationPath $py -Force
  $pth = (Get-ChildItem "$py\python*._pth" | Select-Object -First 1).FullName
  (Get-Content $pth) -replace '^#\s*import site', 'import site' | Set-Content $pth
  Add-Content $pth '..'
  Invoke-WebRequest https://bootstrap.pypa.io/get-pip.py -OutFile "$cache\get-pip.py"
  & "$py\python.exe" "$cache\get-pip.py" --no-warn-script-location
  if ($LASTEXITCODE) { throw "pip could not be set up" }
  # some pywebview helpers only ship as source -> setuptools/wheel are needed to build them
  & "$py\python.exe" -m pip install --no-warn-script-location setuptools wheel
  if ($LASTEXITCODE) { throw "setuptools could not be installed" }
  & "$py\python.exe" -m pip install --no-warn-script-location "pywebview>=5,<7"
  if ($LASTEXITCODE) { throw "pywebview could not be installed" }
  & "$py\python.exe" -m pip uninstall -y pip setuptools wheel | Out-Null
}
& "$py\python.exe" -c "import webview, clr; print('  pywebview ok')"
if ($LASTEXITCODE) { throw "pywebview does not load" }
Copy-Item $py -Destination "$app\python" -Recurse
Get-ChildItem "$app\python" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Get-ChildItem "$app\python\Scripts" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Write-Host "  [2/4] Python + pywebview ready"

# 3. Inno Setup (portable copy inside build\cache unless it is already installed)
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "$cache\innosetup\ISCC.exe") |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  Invoke-WebRequest https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe -OutFile "$cache\is.exe"
  $dir = (Resolve-Path $cache).Path + "\innosetup"
  $p = Start-Process "$cache\is.exe" -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS", "/PORTABLE=1", "/DIR=`"$dir`"" -Wait -PassThru
  $iscc = "$dir\ISCC.exe"
  if (-not (Test-Path $iscc)) { throw "Inno Setup could not be set up" }
}
Write-Host "  [3/4] Inno Setup ready"

# 4. the setup
Remove-Item dist\*.exe -ErrorAction SilentlyContinue
& $iscc /Q "/DMyVersion=$ver" "/DMyNumVersion=$num" installer\osu-review.iss
if ($LASTEXITCODE) { throw "building the setup failed" }
$exe = Get-Item "dist\osu-review-Setup.exe"
Write-Host ("  [4/4] done: {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
if ($env:GITHUB_ENV) { "VERSION=$ver" | Out-File -FilePath $env:GITHUB_ENV -Append }
try { Stop-Transcript | Out-Null } catch {}
