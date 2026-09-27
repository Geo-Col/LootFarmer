# Loot Farmer - one-time setup. Run via Setup.bat (double-click). Safe to run again: finished steps are skipped.
param([switch]$Check)   # -Check: only report what's missing, change nothing

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"   # PowerShell's progress bar makes downloads many times slower

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   OK  $t" -ForegroundColor Green }
function Todo($t) { Write-Host "   ->  $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "   !!  $t" -ForegroundColor Red }
function Ask($q)  { if ($Check) { return $false }; return (Read-Host "   $q [y/n]") -match '^[yY]' }
function HaveWinget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Winget($id, $extra) {
    if (-not (HaveWinget)) { Todo "winget isn't available on this PC - using a direct download instead."; return }
    try { & winget install -e --id $id --accept-package-agreements --accept-source-agreements @extra }
    catch { Todo "winget failed ($_) - using a direct download instead." }
}
function Download($url, $dest) {
    Todo "downloading $(Split-Path $dest -Leaf) ..."
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

# Any unexpected error: show it and wait, instead of the window just vanishing.
trap {
    Bad "Setup stopped with an error: $_"
    Bad "Take a screenshot of this window, then run Setup.bat again (finished steps are skipped)."
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Loot Farmer setup" -ForegroundColor White
if ($Check) { Write-Host "(check mode: nothing will be changed)" -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 1. Python
Step "Python"
function FindPython {
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                     "C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p -and $p.Source -notlike "*WindowsApps*") { return $p.Source }   # skip the Microsoft Store stub
    return $null
}
$Py = FindPython
if ($Py) { Ok "found $Py" }
elseif ($Check) { Todo "Python will be installed" }
else {
    Todo "installing Python 3.12 (for this user)..."
    Winget "Python.Python.3.12" @("--scope", "user")
    $Py = FindPython
    if (-not $Py) { Bad "Python didn't install. Install it from python.org, then run Setup again."; Read-Host "Press Enter to exit"; exit 1 }
    Ok "installed $Py"
}
$PyW = if ($Py) { Join-Path (Split-Path $Py) "pythonw.exe" } else { $null }

# ---------------------------------------------------------------- 2. Python packages
Step "Python packages"
if ($Py -and -not $Check) {
    & $Py -m pip install --upgrade --quiet pip
    & $Py -m pip install --quiet opencv-python Pillow numpy sv-ttk pytesseract groq
    if ($LASTEXITCODE -ne 0) { Bad "pip install failed - check the internet connection and run Setup again." } else { Ok "installed" }
} else { Todo "opencv-python, Pillow, numpy, sv-ttk, pytesseract, groq" }

# ---------------------------------------------------------------- 3. Tesseract OCR
Step "Tesseract OCR"
$Tess = "C:\Program Files\Tesseract-OCR\tesseract.exe"
if (Test-Path "$Here\Tesseract-OCR\tesseract.exe") { $Tess = "$Here\Tesseract-OCR\tesseract.exe"; Ok "included in the bot folder" }
elseif (Test-Path $Tess) { Ok "found" }
elseif ($Check) { Todo "Tesseract will be installed" }
else {
    Todo "installing (Windows may ask for permission)..."
    Winget "UB-Mannheim.TesseractOCR" @()
    if (-not (Test-Path $Tess)) {
        # Fallback: the official Windows installer straight from UB-Mannheim's GitHub releases, installed silently.
        $rel = Invoke-RestMethod "https://api.github.com/repos/UB-Mannheim/tesseract/releases/latest" -UseBasicParsing
        $asset = $rel.assets | Where-Object { $_.name -like "*w64-setup*.exe" } | Select-Object -First 1
        $exe = "$env:TEMP\$($asset.name)"
        Download $asset.browser_download_url $exe
        Todo "running the Tesseract installer - click Yes if Windows asks for permission..."
        Start-Process -FilePath $exe -ArgumentList "/S" -Verb RunAs -Wait
        Remove-Item $exe -ErrorAction SilentlyContinue
    }
    if (Test-Path $Tess) { Ok "installed" }
    else { Bad "still not found - install it by hand from github.com/UB-Mannheim/tesseract/wiki, then run Setup again." }
}

# ---------------------------------------------------------------- 4. ADB (platform-tools)
Step "ADB (Android platform-tools)"
$Adb = @("$Here\platform-tools\adb.exe", "C:\Program Files\platform-tools\adb.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Adb) { Ok "found $Adb" }
elseif ($Check) { Todo "platform-tools will be downloaded into this folder" }
else {
    $zip = "$env:TEMP\platform-tools.zip"
    Download "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" $zip
    Expand-Archive -Path $zip -DestinationPath $Here -Force
    Remove-Item $zip
    $Adb = "$Here\platform-tools\adb.exe"
    Ok "installed $Adb"
}

# ---------------------------------------------------------------- 5. cloudflared (phone link)
Step "cloudflared (phone link from anywhere)"
if (Test-Path "$Here\cloudflared.exe") { Ok "found" }
elseif ($Check) { Todo "cloudflared.exe will be downloaded" }
else {
    Download "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" "$Here\cloudflared.exe"
    $sig = Get-AuthenticodeSignature "$Here\cloudflared.exe"
    if ($sig.Status -eq "Valid" -and $sig.SignerCertificate.Subject -like "*Cloudflare*") { Ok "downloaded (signed by Cloudflare)" }
    else { Remove-Item "$Here\cloudflared.exe"; Bad "signature check failed - removed it. The phone link will be home-Wi-Fi only." }
}

# ---------------------------------------------------------------- 6. BlueStacks
Step "BlueStacks"
$Player = "C:\Program Files\BlueStacks_nxt\HD-Player.exe"
$Conf = "C:\ProgramData\BlueStacks_nxt\bluestacks.conf"
$AdbPort = "5555"
if (-not (Test-Path $Player) -or -not (Test-Path $Conf)) {
    Bad "BlueStacks 5 isn't installed."
    Todo "Install it from https://www.bluestacks.com, open it once, install Clash of Clans from the Play Store,"
    Todo "log in to your account(s), then run Setup again to finish."
} else {
    Ok "found"
    $text = Get-Content $Conf -Raw
    $want = [ordered]@{ 'bst\.enable_adb_access="[^"]*"' = 'bst.enable_adb_access="1"'
                        '(bst\.instance\.[^.]+\.fb_width)="[^"]*"' = '$1="1920"'
                        '(bst\.instance\.[^.]+\.fb_height)="[^"]*"' = '$1="1080"'
                        '(bst\.instance\.[^.]+\.dpi)="[^"]*"' = '$1="240"'
                        '(bst\.instance\.[^.]+\.device_profile_code)="[^"]*"' = '$1="sttu"' }   # Samsung Galaxy S22 Ultra
    $new = $text
    foreach ($k in $want.Keys) { $new = [regex]::Replace($new, $k, $want[$k]) }
    if ($new -eq $text) { Ok "ADB on, 1920x1080, dpi 240, Galaxy S22 Ultra profile" }
    elseif ($Check) { Todo "will turn on ADB, set 1920x1080 / dpi 240 and the Galaxy S22 Ultra profile" }
    else {
        if (Get-Process HD-Player -ErrorAction SilentlyContinue) {
            if (Ask "BlueStacks must be closed to change its settings. Close it now?") { Stop-Process -Name HD-Player -Force; Start-Sleep 3 }
        }
        if (Get-Process HD-Player -ErrorAction SilentlyContinue) { Bad "BlueStacks is still open - settings not changed. Close it and run Setup again." }
        else {
            Copy-Item $Conf "$Conf.lootfarmer-backup" -Force
            [IO.File]::WriteAllText($Conf, $new)   # no BOM: BlueStacks must still read it
            Ok "ADB on, 1920x1080, dpi 240, Galaxy S22 Ultra (backup: bluestacks.conf.lootfarmer-backup)"
        }
    }
    $m = [regex]::Match($new, 'bst\.instance\.Pie64\.status\.adb_port="(\d+)"')
    if (-not $m.Success) { $m = [regex]::Match($new, 'bst\.instance\.[^.]+\.adb_port="(\d+)"') }
    if ($m.Success) { $AdbPort = $m.Groups[1].Value }
}

# ---------------------------------------------------------------- 7. Intel graphics (laptops with two GPUs)
Step "Graphics chip for BlueStacks"
$gpus = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)
$intel = $gpus | Where-Object { $_ -match "Intel" }
$other = $gpus | Where-Object { $_ -match "NVIDIA|AMD|Radeon" }
$key = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
$cur = (Get-ItemProperty -Path $key -Name $Player -ErrorAction SilentlyContinue).$Player
if (-not ($intel -and $other)) { Ok "one graphics chip ($($gpus -join ', ')) - nothing to choose" }
elseif ($cur -eq "GpuPreference=1;") { Ok "BlueStacks already uses the power-saving (Intel) chip" }
elseif (Ask "Run BlueStacks on the Intel chip? Fixes BlueStacks crashing on the loading clouds on NVIDIA laptops") {
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name $Player -Value "GpuPreference=1;"
    Ok "set (takes effect next time BlueStacks starts)"
} else { Todo "left as it is (Windows Settings > Display > Graphics can change it later)" }

# ---------------------------------------------------------------- 8. Bot config
Step "Bot config"
if ($Py -and -not $Check) {
    $env:LF_ADB = $Adb; $env:LF_TESS = $Tess; $env:LF_PLAYER = $Player; $env:LF_TARGET = "127.0.0.1:$AdbPort"
    & $Py -c @"
import json, os
p = 'config.json'
c = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else {}
c.update(adb_path=os.environ['LF_ADB'] or c.get('adb_path', ''), tesseract_path=os.environ['LF_TESS'],
         auto_connect_target=os.environ['LF_TARGET'], device='')
if os.path.exists(os.environ['LF_PLAYER']):
    c.update(emulator_exe_path=os.environ['LF_PLAYER'], watchdog_enabled=True)
json.dump(c, open(p, 'w', encoding='utf-8'), indent=2)
"@
    Ok "paths filled in (ADB target 127.0.0.1:$AdbPort)"
} else { Todo "will fill in the adb / tesseract / BlueStacks paths" }

# ---------------------------------------------------------------- 9. Desktop shortcut
Step "Desktop shortcut"
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Loot Farmer.lnk"
if (Test-Path $lnk) { Ok "exists" }
elseif ($Check -or -not $PyW) { Todo "will create 'Loot Farmer' on the desktop" }
else {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $PyW; $s.Arguments = "`"$Here\bot.py`""; $s.WorkingDirectory = $Here
    $s.Save()
    Ok "created"
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Next: open BlueStacks, start Clash of Clans on the home village, open 'Loot Farmer' from the desktop,"
Write-Host "check Setup > 'Run setup check', then press Start farming."
if (-not $Check) { Read-Host "`nPress Enter to close" }
