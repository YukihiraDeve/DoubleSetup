# SetupDouble – installation sur le PC Windows.
# À lancer dans PowerShell **en administrateur**, depuis le dossier du projet :
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#
# Ce script :
#   1. crée un environnement Python (.venv) et installe les dépendances
#   2. crée config.toml avec un secret aléatoire (à recopier sur le Mac)
#   3. ouvre les ports dans le pare-feu (réseau privé uniquement)
#   4. lance SetupDouble à l'ouverture de session (droits élevés, pour que
#      le raccourci marche aussi quand une appli admin est au premier plan)
#   5. ajoute « SetupDouble » au menu Démarrer (fenêtre de statut)

param(
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Relance ce script dans un PowerShell 'Exécuter en tant qu'administrateur'." -ForegroundColor Red
    exit 1
}

Write-Host "== 1/5 Environnement Python" -ForegroundColor Cyan
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv --python 3.12 .venv
    uv pip install --python $venvPy -r requirements-win.txt
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) {
        Write-Host "Python introuvable. Installe-le avec :  winget install Python.Python.3.12" -ForegroundColor Red
        exit 1
    }
    py -3 -m venv .venv
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r requirements-win.txt
}

Write-Host "== 2/5 Configuration" -ForegroundColor Cyan
$cfg = Join-Path $Root "config.toml"
if (-not (Test-Path $cfg)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "B").Replace("=", "")
    (Get-Content (Join-Path $Root "config.example.toml") -Raw -Encoding UTF8).Replace(
        'secret = "change-me"', "secret = `"$secret`"") |
        Set-Content $cfg -Encoding UTF8
    Write-Host "config.toml créé. Secret à recopier sur le Mac :" -ForegroundColor Yellow
    Write-Host "    $secret" -ForegroundColor Yellow
} else {
    Write-Host "config.toml existe déjà, conservé."
}

Write-Host "== 3/5 Pare-feu (réseau privé)" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName "SetupDouble*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "SetupDouble contrôle (TCP 47801)" -Direction Inbound `
    -Protocol TCP -LocalPort 47801 -Action Allow -Profile Private | Out-Null
New-NetFirewallRule -DisplayName "SetupDouble audio (UDP 47802)" -Direction Inbound `
    -Protocol UDP -LocalPort 47802 -Action Allow -Profile Private | Out-Null
$netCategory = (Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory
if ($netCategory -ne "Private") {
    Write-Host "Attention : ton réseau est en mode '$netCategory'. Passe-le en 'Privé' dans" -ForegroundColor Yellow
    Write-Host "Paramètres > Réseau et Internet > (ta connexion) > Type de profil réseau." -ForegroundColor Yellow
}

Write-Host "== 4/5 Démarrage automatique" -ForegroundColor Cyan
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$taskName = "SetupDouble"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
if (-not $NoAutostart) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m setupdouble.server_win" `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "SetupDouble démarré (icône près de l'horloge) et lancé à chaque ouverture de session."
} else {
    Write-Host "Lancement manuel :  .venv\Scripts\pythonw.exe -m setupdouble.server_win"
}

Write-Host "== 5/5 Raccourci dans le menu Démarrer" -ForegroundColor Cyan
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $startMenu "SetupDouble.lnk"))
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m setupdouble.app"
$lnk.WorkingDirectory = $Root
$lnk.Description = "Statut de SetupDouble (clavier, souris, audio)"
$lnk.Save()
Write-Host "Fenêtre de statut : menu Démarrer > SetupDouble, ou clic droit sur l'icône > Ouvrir SetupDouble."

Write-Host ""
Write-Host "Périphériques audio détectés (vérifie que la Scarlett apparaît) :" -ForegroundColor Cyan
& $venvPy -m setupdouble.devices
Write-Host ""
Write-Host "Terminé. Raccourci par défaut : Ctrl+Alt+Espace." -ForegroundColor Green
