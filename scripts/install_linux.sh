#!/usr/bin/env bash
# SetupDouble – installation sur Linux (appelé par ./install.sh).
#   ./scripts/install_linux.sh [--role client|server] [--secret CLÉ] [--no-autostart]
#
#   client (défaut) : cette machine est pilotée par le clavier/la souris d'une autre
#   server          : clavier, souris et carte son sont branchés ici
# Fonctionne sous X11 et Wayland (accès direct au noyau : uinput / evdev).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ROLE="client"
SECRET=""
AUTOSTART=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --no-autostart) AUTOSTART=0; shift ;;
    *) echo "option inconnue : $1"; exit 1 ;;
  esac
done
[[ "$ROLE" == "client" || "$ROLE" == "server" ]] || { echo "--role : client ou server"; exit 1; }

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

GENERATED=0
if [[ ! -f config.toml ]]; then
  if [[ "$ROLE" == "server" && -z "$SECRET" ]]; then
    SECRET="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
    GENERATED=1
  fi
  if [[ -z "$SECRET" ]]; then
    read -r -p "Colle la clé affichée par l'installation du serveur : " SECRET
  fi
  [[ ${#SECRET} -ge 12 && "$SECRET" =~ ^[A-Za-z0-9_.-]+$ ]] || {
    echo "Clé invalide : 12 caractères minimum (lettres, chiffres, '.', '_' ou '-')."; exit 1; }
fi

say "1/5 Paquets système (mot de passe administrateur)"
if command -v apt-get >/dev/null; then
  sudo apt-get install -y libportaudio2 curl wl-clipboard xclip
elif command -v dnf >/dev/null; then
  sudo dnf install -y portaudio curl wl-clipboard xclip
elif command -v pacman >/dev/null; then
  sudo pacman -S --needed --noconfirm portaudio curl wl-clipboard xclip
elif command -v zypper >/dev/null; then
  sudo zypper install -y portaudio curl wl-clipboard xclip
else
  echo "Gestionnaire de paquets inconnu : installe PortAudio et wl-clipboard ou xclip toi-même."
fi

say "2/5 Accès au clavier et à la souris (uinput / evdev)"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-setupdouble-uinput.rules >/dev/null
echo uinput | sudo tee /etc/modules-load.d/setupdouble-uinput.conf >/dev/null
sudo modprobe uinput || true
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=uinput || true
NEED_RELOGIN=0
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  sudo usermod -aG input "$USER"
  NEED_RELOGIN=1
fi
if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  if [[ "$ROLE" == "server" ]]; then
    sudo ufw allow 47801/tcp && sudo ufw allow 47802/udp
  else
    sudo ufw allow 47800/udp && sudo ufw allow 47803/udp
  fi
fi

say "3/5 Environnement Python"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv venv --allow-existing --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-linux.txt

say "4/5 Configuration"
if [[ ! -f config.toml ]]; then
  sed -e "s|^secret = \"change-me\"|secret = \"${SECRET}\"|" \
      -e "s|^role = \"server\"|role = \"${ROLE}\"|" config.example.toml > config.toml
  chmod 600 config.toml
  echo "config.toml créé (rôle : $ROLE)."
else
  .venv/bin/python -c "import sys; from setupdouble import config; config.save({None: {'role': sys.argv[1]}})" "$ROLE"
  echo "config.toml conservé (rôle : $ROLE)."
fi

say "5/5 Démarrage automatique (service utilisateur systemd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/setupdouble.service" <<EOF
[Unit]
Description=SetupDouble ($ROLE)
After=graphical-session.target network-online.target
PartOf=graphical-session.target

[Service]
WorkingDirectory=$ROOT
Environment=SETUPDOUBLE_CONFIG=$ROOT/config.toml
ExecStart=$ROOT/.venv/bin/python -m setupdouble $ROLE --no-tray
Restart=always
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF
if [[ "$ROLE" == "client" ]]; then
  sed -i "s| --no-tray||" "$UNIT_DIR/setupdouble.service"
fi
systemctl --user daemon-reload
if [[ $AUTOSTART == 1 ]]; then
  systemctl --user enable setupdouble.service
  if [[ $NEED_RELOGIN == 0 ]]; then systemctl --user restart setupdouble.service; fi
fi

echo
echo "====================================================================="
if [[ $GENERATED == 1 ]]; then
  echo "Clé à recopier sur l'autre machine (ou dans ses Réglages → Connexion) :"
  echo
  echo "      $SECRET"
  echo
fi
if [[ $NEED_RELOGIN == 1 ]]; then
  echo "Déconnecte-toi puis reconnecte-toi (ajout au groupe « input ») :"
  echo "SetupDouble démarrera tout seul ensuite."
fi
cat <<EOF
Fenêtre de statut :  $ROOT/.venv/bin/python -m setupdouble app
Journal :            ~/.local/state/setupdouble/$ROLE.log
EOF
echo "====================================================================="
