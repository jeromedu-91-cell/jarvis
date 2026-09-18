#!/bin/bash
# JARVIS_START_DEBUG.command — Boot Screen avec console visible (diagnostic).
# JARVIS tourne AU PREMIER PLAN : logs réels dans ce Terminal, Ctrl+C pour
# arrêter proprement.
cd "$(dirname "$0")"
export PYTHONUTF8=1
export JARVIS_BOOT_DEBUG=1

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

echo "Mode console : JARVIS tourne ici, logs visibles. Ctrl+C pour arrêter."
"$PY" -m jarvis.boot --console "$@"
rc=$?
if [ "$rc" -eq 0 ] || [ "$rc" -eq 130 ]; then
  exit 0
fi
echo
echo "JARVIS START FAILED (code $rc)."
echo "Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé."
exec bash