$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "setup.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Alert($msg) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($msg, "YunKe Assistant", "OK", "Information") | Out-Null
    } catch { }
}

try {
    Log "===== setup start ====="
    Set-Location $root

    $python = $null
    foreach ($cand in @("python", "py")) {
        try {
            $out = & $cand -V 2>&1
            if ($LASTEXITCODE -eq 0 -and $out -match "Python 3") { $python = $cand; break }
        } catch { }
    }
    if (-not $python) {
        Log "python not found, try winget"
        winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
    }
    if (-not $python) { throw "Python 3 not found. Please install Python 3.11+ from python.org and run setup.bat again." }
    Log "python = $python"

    $venv = Join-Path $root ".venv"
    $vpy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        Log "creating venv"
        & $python -m venv $venv
    }
    if (-not (Test-Path $vpy)) { throw "Failed to create virtual environment." }

    Log "installing dependencies"
    & $vpy -m pip install --upgrade pip --disable-pip-version-check | Out-Null
    & $vpy -m pip install -r (Join-Path $root "requirements.txt") --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Log "retry with tsinghua mirror"
        & $vpy -m pip install -r (Join-Path $root "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Python packages. See data\setup.log" }

    $ff = Join-Path $root "bin\ffmpeg.exe"
    if (-not (Test-Path $ff)) {
        if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
            Log "system ffmpeg found in PATH"
        } else {
            Log "downloading ffmpeg (about 80MB)"
            $zip = Join-Path $env:TEMP "ykzs_ffmpeg.zip"
            $dst = Join-Path $env:TEMP "ykzs_ffmpeg"
            try {
                Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -UseBasicParsing
                if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
                Expand-Archive -Path $zip -DestinationPath $dst -Force
                $exe = Get-ChildItem $dst -Recurse -Filter ffmpeg.exe | Select-Object -First 1
                $probe = Get-ChildItem $dst -Recurse -Filter ffprobe.exe | Select-Object -First 1
                New-Item -ItemType Directory -Force -Path (Join-Path $root "bin") | Out-Null
                Copy-Item $exe.FullName (Join-Path $root "bin\ffmpeg.exe") -Force
                Copy-Item $probe.FullName (Join-Path $root "bin\ffprobe.exe") -Force
                Log "ffmpeg installed to bin"
            } catch {
                Log "ffmpeg download failed: $($_.Exception.Message)"
            }
        }
    }

    Log "creating desktop shortcut"
    try {
        $ws = New-Object -ComObject WScript.Shell
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnk = $ws.CreateShortcut((Join-Path $desktop "YunKe Assistant.lnk"))
        $lnk.TargetPath = Join-Path $root "start.bat"
        $lnk.WorkingDirectory = $root
        $icon = Join-Path $root "assets\icon.ico"
        if (Test-Path $icon) { $lnk.IconLocation = $icon }
        $lnk.Description = "Video course assistant: bilingual subtitles, segment summaries, AI QA"
        $lnk.Save()
    } catch { Log "shortcut failed: $($_.Exception.Message)" }

    Log "launching app"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$(Join-Path $root 'start.bat')`"" -WindowStyle Hidden
    Log "===== setup done ====="
    Alert "Install finished. The app is starting now.`nLog: $log"
} catch {
    Log "ERROR: $($_.Exception.Message)"
    Alert "Setup failed: $($_.Exception.Message)`nPlease check: $log"
}
