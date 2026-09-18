#!/bin/bash
# JARVIS_STOP.command — arrêt PROPRE de JARVIS (SIGINT vers SON PID confirmé).
# Jamais de kill forcé : un autre processus sur le port 8765 est signalé, pas tué.
cd "$(dirname "$0")"
export PYTHONUTF8=1

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ introuvable : JARVIS requiert 3.10 ou plus."
  exec bash
fi

"$PY" -m jarvis.startup --stop "$@"
echo
echo "Ferme cette fenêtre pour terminer."
exec bash