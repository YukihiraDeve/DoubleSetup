# SetupDouble – installation sous Windows (appelé par install.ps1, qui demande les droits).
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 [-Role server|client] [-Secret CLÉ]
#
#   server (défaut) : clavier, souris et carte son sont branchés sur ce PC
#   client          : ce PC est piloté par le clavier/la souris d'une autre machine
#
# Étapes : Python (.venv), config.toml, pare-feu (réseau privé), démarrage
# automatique (droits élevés), raccourci « SetupDouble » dans le menu Démarrer.

param(
    [ValidateSet("server", "client")][string]$Role = "server",
    [string]$Secret = "",
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Lance plutôt install.ps1 (il demande les droits administrateur tout seul)." -ForegroundColor Red
    exit 1
}

# --- La clé est vérifiée AVANT les installations
$cfg = Join-Path $Root "config.toml"
$generated = $false
if (-not (Test-Path $cfg)) {
    if ($Role -eq "server" -and -not $Secret) {
        $bytes = New-Object byte[] 24
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $Secret = [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "B").Replace("=", "")
        $generated = $true
    }
    if (-not $Secret) { $Secret = Read-Host "Colle la clé affichée par l'installation du serveur" }
    if ($Secret -notmatch '^[A-Za-z0-9_.\-]{12,128}$') {
        Write-Host "Clé invalide : 12 caractères minimum (lettres, chiffres, '.', '_' ou '-')." -ForegroundColor Red
        exit 1
    }
}

Write-Host "== 1/5 Environnement Python" -ForegroundColor Cyan
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements | Out-Null
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
    }
}
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv --allow-existing --python 3.12 .venv
    uv pip install --python $venvPy -r requirements-win.txt
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) {
        Write-Host "Python introuvable. Installe-le avec :  winget install Python.Python.3.12" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $venvPy)) { py -3 -m venv .venv }
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r requirements-win.txt
}

Write-Host "== 2/5 Configuration" -ForegroundColor Cyan
if (-not (Test-Path $cfg)) {
    $text = (Get-Content (Join-Path $Root "config.example.toml") -Raw -Encoding UTF8)
    $text = $text.Replace('secret = "change-me"', "secret = `"$Secret`"").Replace('role = "server"', "role = `"$Role`"")
    [IO.File]::WriteAllText($cfg, $text, (New-Object Text.UTF8Encoding($false)))
    Write-Host "config.toml créé (rôle : $Role)."
} else {
    & $venvPy -c "import sys; from setupdouble import config; config.save({None: {'role': sys.argv[1]}})" $Role
    Write-Host "config.toml conservé (rôle : $Role)."
}

Write-Host "== 3/5 Pare-feu (réseau privé)" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName "SetupDouble*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Role -eq "server") {
    New-NetFirewallRule -DisplayName "SetupDouble contrôle (TCP 47801)" -Direction Inbound `
        -Protocol TCP -LocalPort 47801 -Action Allow -Profile Private | Out-Null
    New-NetFirewallRule -DisplayName "SetupDouble audio (UDP 47802)" -Direction Inbound `
        -Protocol UDP -LocalPort 47802 -Action Allow -Profile Private | Out-Null
} else {
    New-NetFirewallRule -DisplayName "SetupDouble découverte (UDP 47800)" -Direction Inbound `
        -Protocol UDP -LocalPort 47800 -Action Allow -Profile Private | Out-Null
    New-NetFirewallRule -DisplayName "SetupDouble audio (UDP 47803)" -Direction Inbound `
        -Protocol UDP -LocalPort 47803 -Action Allow -Profile Private | Out-Null
}
$netCategory = (Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory
if ($netCategory -ne "Private") {
    Write-Host "Attention : ton réseau est en mode '$netCategory'. Passe-le en 'Privé' dans" -ForegroundColor Yellow
    Write-Host "Paramètres > Réseau et Internet > (ta connexion) > Type de profil réseau." -ForegroundColor Yellow
}

Write-Host "== 4/5 Démarrage automatique" -ForegroundColor Cyan
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$taskName = "SetupDouble"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
if (-not $NoAutostart) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m setupdouble.$Role" `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "SetupDouble démarré, et relancé à chaque ouverture de session."
} else {
    Write-Host "Lancement manuel :  .venv\Scripts\pythonw.exe -m setupdouble $Role"
}

Write-Host "== 5/5 Menu Démarrer" -ForegroundColor Cyan
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $startMenu "SetupDouble.lnk"))
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m setupdouble.app"
$lnk.WorkingDirectory = $Root
$lnk.Description = "Statut et réglages de SetupDouble"
$lnk.Save()

Write-Host ""
Write-Host "====================================================================="
if ($generated) {
    Write-Host "Clé à recopier sur l'autre machine (ou dans ses Réglages > Connexion) :" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "      $Secret" -ForegroundColor Yellow
    Write-Host ""
}
Write-Host "Fenêtre de statut et réglages : menu Démarrer > SetupDouble."
if ($Role -eq "client") {
    Write-Host "Pour recevoir le micro du serveur : installe VB-CABLE (https://vb-audio.com/Cable/)."
}
Write-Host "Raccourci par défaut : Ctrl+Alt+Espace (modifiable dans les Réglages)." -ForegroundColor Green
Write-Host "====================================================================="
