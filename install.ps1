# SetupDouble – installation Windows en un clic.
# Clic droit sur ce fichier > « Exécuter avec PowerShell », ou :
#   powershell -ExecutionPolicy Bypass -File install.ps1
param([ValidateSet("", "server", "client")][string]$Role = "", [string]$Secret = "")

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $Role) {
    Write-Host ""
    Write-Host "  SetupDouble - que fait ce PC ?" -ForegroundColor Cyan
    Write-Host "  [1] Mon clavier, ma souris et ma carte son sont branchés ICI (serveur)"
    Write-Host "  [2] Ce PC est piloté par le clavier d'une autre machine (piloté)"
    $choice = Read-Host "  Ton choix [1]"
    $Role = if ($choice -eq "2") { "client" } else { "server" }
}
if ($Role -eq "client" -and -not $Secret -and -not (Test-Path (Join-Path $here "config.toml"))) {
    $Secret = Read-Host "  Colle la clé affichée par l'installation du serveur"
}

$installer = Join-Path $here "scripts\install_windows.ps1"
$extra = @("-Role", $Role)
if ($Secret) { $extra += @("-Secret", $Secret) }

if ($isAdmin) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer @extra
} else {
    # demande les droits administrateur (pare-feu, démarrage automatique)
    $line = "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$installer`" " + ($extra -join " ")
    Start-Process powershell -Verb RunAs -ArgumentList $line
}
