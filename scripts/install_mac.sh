#!/usr/bin/env bash
# SetupDouble – installation sur le Mac.
#   ./scripts/install_mac.sh [--secret LE_SECRET_DU_PC] [--no-audio] [--no-autostart]
#
# Ce script :
#   1. installe BlackHole 2ch + 16ch et SwitchAudioSource (Homebrew)
#   2. crée l'environnement Python (.venv) et installe les dépendances
#   3. crée config.toml avec le secret du PC
#   4. installe un LaunchAgent : l'agent démarre à l'ouverture de session
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SECRET=""
AUDIO=1
AUTOSTART=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --secret) SECRET="$2"; shift 2 ;;
    --no-audio) AUDIO=0; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "option inconnue : $1"; exit 1 ;;
  esac
done

command -v brew >/dev/null || { echo "Homebrew est requis : https://brew.sh"; exit 1; }

if [[ $AUDIO == 1 ]]; then
  echo "== 1/4 Pilotes audio virtuels (mot de passe administrateur demandé par macOS)"
  brew list --cask blackhole-2ch >/dev/null 2>&1 || brew install --cask blackhole-2ch
  brew list --cask blackhole-16ch >/dev/null 2>&1 || brew install --cask blackhole-16ch
  brew list switchaudio-osx >/dev/null 2>&1 || brew install switchaudio-osx
else
  echo "== 1/4 Audio ignoré (--no-audio)"
fi

echo "== 2/4 Environnement Python"
if ! command -v uv >/dev/null; then brew install uv; fi
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-mac.txt

echo "== 3/4 Configuration"
if [[ ! -f config.toml ]]; then
  if [[ -z "$SECRET" ]]; then
    read -r -p "Colle le secret affiché par l'installation Windows : " SECRET
  fi
  [[ ${#SECRET} -ge 12 ]] || { echo "Secret trop court"; exit 1; }
  sed "s|^secret = \"change-me\"|secret = \"${SECRET}\"|" config.example.toml > config.toml
  if [[ $AUDIO == 0 ]]; then
    sed -i '' 's/^enabled = true/enabled = false/' config.toml
  fi
  echo "config.toml créé."
else
  echo "config.toml existe déjà, conservé."
fi

PY_REAL="$(.venv/bin/python -c 'import os,sys; print(os.path.realpath(sys.executable))')"
SITE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PLIST="$HOME/Library/LaunchAgents/com.setupdouble.agent.plist"
LOGDIR="$HOME/Library/Logs/SetupDouble"
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

echo "== 4/4 Démarrage automatique"
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
    <string>setupdouble.client_mac</string>
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
  echo "Agent lancé et démarré à chaque ouverture de session."
else
  echo "Lancement manuel :  .venv/bin/python -m setupdouble.client_mac"
fi

cat <<EOF

=====================================================================
Il reste UNE étape à faire toi-même (macOS ne permet pas de l'automatiser) :

  Réglages Système → Confidentialité et sécurité → Accessibilité
  → bouton « + » → Cmd+Maj+G → colle ce chemin, puis active l'interrupteur :

      ${PY_REAL}

Puis redémarre l'agent :
      launchctl kickstart -k gui/$(id -u)/com.setupdouble.agent

Conseil clavier : ajoute la disposition « Français – PC » dans
Réglages Système → Clavier → Méthodes de saisie, pour que AltGr, @, #, {…}
tombent exactement comme sur Windows.

Journal : ${LOGDIR}/client.log
=====================================================================
EOF
