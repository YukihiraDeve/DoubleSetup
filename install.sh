#!/usr/bin/env bash
# SetupDouble – installation en une commande sur macOS et Linux :
#   ./install.sh                      (pose deux questions)
#   ./install.sh --role client --secret CLÉ
set -euo pipefail
cd "$(dirname "$0")"

ROLE=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

if [[ -z "$ROLE" ]]; then
  echo
  echo "  SetupDouble – que fait cet ordinateur ?"
  echo "  [1] Il est piloté par le clavier/la souris d'une autre machine (piloté)"
  echo "  [2] Mon clavier, ma souris et ma carte son sont branchés ICI (serveur)"
  read -r -p "  Ton choix [1] : " CHOICE
  if [[ "$CHOICE" == "2" ]]; then ROLE=server; else ROLE=client; fi
fi

case "$(uname -s)" in
  Darwin) exec ./scripts/install_mac.sh --role "$ROLE" ${ARGS[@]+"${ARGS[@]}"} ;;
  Linux)  exec ./scripts/install_linux.sh --role "$ROLE" ${ARGS[@]+"${ARGS[@]}"} ;;
  *) echo "Sous Windows, lance install.ps1."; exit 1 ;;
esac
