#!/usr/bin/env bash
# SetupDouble – installation sur macOS (appelé par ./install.sh).
#   ./scripts/install_mac.sh [--role client|server] [--secret CLÉ] [--no-audio] [--no-autostart]
#
#   client (défaut) : ce Mac est piloté par le clavier/la souris d'une autre machine
#   server          : clavier, souris et carte son sont branchés sur ce Mac
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ROLE="client"
SECRET=""
AUDIO=1
AUTOSTART=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --no-audio) AUDIO=0; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "option inconnue : $1"; exit 1 ;;
  esac
done
[[ "$ROLE" == "client" || "$ROLE" == "server" ]] || { echo "--role : client ou server"; exit 1; }

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

# --- La clé est vérifiée AVANT les installations, pour ne pas échouer au milieu
GENERATED=0
if [[ ! -f config.toml ]]; then
  if [[ "$ROLE" == "server" && -z "$SECRET" ]]; then
    SECRET="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
    GENERATED=1
  fi
  if [[ -z "$SECRET" ]]; then
    read -r -p "Colle la clé affichée par l'installation du serveur : " SECRET
  fi
  case "$SECRET" in
    LE_SECRET*|change-me|"") echo "Il faut la vraie clé affichée par l'installation du serveur."; exit 1 ;;
  esac
  [[ ${#SECRET} -ge 12 ]] || { echo "Clé trop courte (12 caractères minimum)."; exit 1; }
  [[ "$SECRET" =~ ^[A-Za-z0-9_.-]+$ ]] || {
    echo "La clé ne doit contenir que des lettres, chiffres, '.', '_' ou '-'."; exit 1; }
fi

AUDIO_DRIVERS=$AUDIO
if ! command -v brew >/dev/null; then
  echo "Homebrew (https://brew.sh) est conseillé : sans lui, pas d'audio virtuel BlackHole."
  AUDIO_DRIVERS=0
fi

say "1/5 Pilotes audio et réveil réseau"
if [[ "$ROLE" == "client" && $AUDIO_DRIVERS == 1 ]]; then
  echo "macOS peut demander ton mot de passe pour installer BlackHole."
  brew list --cask blackhole-2ch >/dev/null 2>&1 || brew install --cask blackhole-2ch
  brew list --cask blackhole-16ch >/dev/null 2>&1 || brew install --cask blackhole-16ch
  brew list switchaudio-osx >/dev/null 2>&1 || brew install switchaudio-osx
else
  echo "Pas de pilote audio virtuel nécessaire."
fi
# Bascule de l'entrée de l'écran partagé (DDC/CI)
if command -v brew >/dev/null && ! command -v m1ddc >/dev/null && [[ "$(uname -m)" == "arm64" ]]; then
  brew install m1ddc || echo "m1ddc non installé : la bascule d'écran ne sera pas disponible sur ce Mac."
fi
if [[ "$ROLE" == "client" && "$(pmset -g | awk '$1 == "womp" {print $2}')" != "1" ]]; then
  echo "Activation du réveil par le réseau (mot de passe administrateur)"
  sudo pmset -a womp 1 || echo "Impossible d'activer le réveil par le réseau (sudo pmset -a womp 1)"
fi

say "2/5 Environnement Python"
if ! command -v uv >/dev/null; then
  if command -v brew >/dev/null; then
    brew install uv
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
uv venv --allow-existing --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-mac.txt

say "3/5 Configuration"
if [[ ! -f config.toml ]]; then
  sed -e "s|^secret = \"change-me\"|secret = \"${SECRET}\"|" \
      -e "s|^role = \"server\"|role = \"${ROLE}\"|" config.example.toml > config.toml
  chmod 600 config.toml
  if [[ $AUDIO == 0 ]]; then sed -i '' 's/^enabled = true/enabled = false/' config.toml; fi
  echo "config.toml créé (rôle : $ROLE)."
else
  .venv/bin/python -c "import sys; from setupdouble import config; config.save({None: {'role': sys.argv[1]}})" "$ROLE"
  echo "config.toml conservé (rôle : $ROLE)."
fi

PY_REAL="$(.venv/bin/python -c 'import os,sys; print(os.path.realpath(sys.executable))')"
SITE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PLIST="$HOME/Library/LaunchAgents/com.setupdouble.agent.plist"
LOGDIR="$HOME/Library/Logs/SetupDouble"
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

say "4/5 Démarrage automatique"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
if [[ $AUTOSTART == 1 ]]; then
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.setupdouble.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY_REAL}</string>
    <string>-m</string>
    <string>setupdouble.${ROLE}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>${ROOT}:${SITE}</string>
    <key>SETUPDOUBLE_CONFIG</key><string>${ROOT}/config.toml</string>
  </dict>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>${LOGDIR}/agent.out.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/agent.err.log</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "SetupDouble lancé, et relancé à chaque ouverture de session."
else
  echo "Lancement manuel :  .venv/bin/python -m setupdouble ${ROLE}"
fi

say "5/5 Application SetupDouble (fenêtre de statut)"
.venv/bin/python -m setupdouble.mac_bundle --dest "$HOME/Applications"

echo
echo "====================================================================="
if [[ $GENERATED == 1 ]]; then
  echo "Clé à recopier sur l'autre machine (ou dans ses Réglages → Connexion) :"
  echo
  echo "      $SECRET"
  echo
fi
cat <<EOF
Une seule étape manuelle (macOS l'impose) : autoriser Python.
macOS affiche une demande au démarrage de SetupDouble ; sinon :
  Réglages Système → Confidentialité et sécurité → Accessibilité
  (« Contrôle de l'appareil et accès aux données » sur macOS récent)
  → active « python3.12 ».

Fenêtre de statut : app « SetupDouble » (Spotlight ou ~/Applications).
Journal : ${LOGDIR}/${ROLE}.log
EOF
if [[ "$ROLE" == "client" && $AUDIO_DRIVERS == 1 ]]; then
  echo
  echo "Première installation de BlackHole ? Redémarre le Mac pour que macOS le voie."
fi
echo "====================================================================="
