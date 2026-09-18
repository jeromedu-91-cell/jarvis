#!/bin/bash
# JARVIS_START.command — démarre JARVIS avec le Boot Screen (macOS).
# Double-clic dans Finder : Terminal + Boot Screen Tk, puis navigateur.
# La fenêtre se ferme quand le Boot Screen a fini ; JARVIS continue en tâche
# de fond (voir JARVIS_STOP.command pour l'arrêter).
cd "$(dirname "$0")"
export PYTHONUTF8=1

# Meilleur interpréteur Python disponible (JARVIS requiert 3.10+).
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ introuvable : JARVIS requiert 3.10 ou plus."
  echo "Installe-le depuis python.org, puis relance ce lanceur."
  exec bash
fi

"$PY" -m jarvis.boot "$@"
rc=$?
if [ "$rc" -eq 0 ] || [ "$rc" -eq 130 ]; then
  exit 0
fi
echo
echo "JARVIS START FAILED (code $rc)."
echo "Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé."
exec bash