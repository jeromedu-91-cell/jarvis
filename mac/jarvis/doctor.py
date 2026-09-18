"""Diagnostic d'installation — ce qui marche, ce qui manque, comment le réparer.

JARVIS dépend de briques externes (Blender, ComfyUI, Ollama, Piper, Playwright,
Whisper…). Jusqu'ici, leur absence se manifestait par un message d'erreur isolé
au moment d'appeler l'outil concerné, sans vue d'ensemble ni marche à suivre.

Deux niveaux de diagnostic :

  * LE DOCTOR SYSTÈME (`full_diagnose`) répond d'abord à « pourquoi JARVIS ne
    démarre pas » : Python, dépôt, imports, configuration, secrets, base,
    EventBus, serveur LLM, ports, registre d'outils, Mission Control (lecture
    seule), imports Brainrot (lecture seule), serveur API. Il n'a besoin
    d'AUCUN Core pour tourner, ne modifie rien, et restitue l'erreur exacte
    (type, message, fichier, ligne) plutôt qu'un masquage.
  * LES BRIQUES (`diagnose`) mesurent l'état réel de chaque dépendance externe
    (aucune valeur supposée) ; `repair()` n'installe que ce qui s'installe sans
    décision humaine. Tout ce qui demande un choix (quel modèle LLM, quel
    checkpoint SDXL, quelle carte) est signalé, jamais décidé ici.

Le Doctor DIAGNOSTIQUE. Il ne répare jamais un autre chantier (Brainrot,
Mission Control, Discord, LLM, base) — il enregistre le problème et continue.

Usage :
  python -m jarvis.doctor            diagnostic complet, lisible
  python -m jarvis.doctor --json     le même, exploitable par une machine
  python -m jarvis.doctor --strict   code de sortie : 0 rien de bloquant,
                                     1 bloquant, 2 erreur interne du Doctor
  python -m jarvis.doctor --repair   installe les paquets auto-installables
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import queue
import socket
import sqlite3
import subprocess
import sys
import traceback
import contextlib
import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .config import BACKUP_DIR, DATA_DIR, DB_PATH, IS_WINDOWS, LOG_DIR, ROOT

# Sondes réseau courtes : un service mort ne doit jamais bloquer Doctor.
NET_TIMEOUT_S = 3.0
# Serveur LLM local OpenAI-compatible usuel de cette machine. Ce n'est PAS une
# obligation : les endpoints réellement configurés (connecteurs) sont sondés
# aussi, et c'est leur réponse qui fait foi.
DEFAULT_LOCAL_LLM_URL = "http://127.0.0.1:8080/v1"
MAIN_API_PORT = 8765
LLM_API_PORT = 8080

# Statuts du doctor système (distincts des gravités « briques » ci-dessous).
OK = "ok"
WARNING = "warning"
ERROR = "error"

# Profil de santé global du rapport (DEGRADED est déjà la gravité « affaibli »).
HEALTHY = "healthy"
BLOCKED = "blocked"

SYSTEM_LABELS = {
    "python": "Python",
    "repository": "Repository",
    "configuration": "Configuration",
    "secrets": "Secrets",
    "database": "Database",
    "eventbus": "EventBus",
    "imports": "Imports",
    "llm_server": "LLM Server",
    "llm_model": "LLM Model",
    "ports": "Ports",
    "tool_registry": "ToolRegistry",
    "mission_control": "Mission Control",
    "brainrot_imports": "Brainrot imports",
    "api_server": "API Server",
    "core": "Core startup",
    "external_bricks": "Briques externes",
}

# Gravité d'un point de contrôle en échec.
BLOCKING = "blocking"      # une fonctionnalité entière est hors service
DEGRADED = "degraded"      # marche, mais amputé
OPTIONAL = "optional"      # confort


def _module(name: str) -> bool:
    """True si le module est importable, sans l'importer réellement."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _check(name: str, label: str, ok: bool, *, severity: str = DEGRADED,
           detail: str = "", fix: str = "", auto: bool = False) -> dict[str, Any]:
    return {"name": name, "label": label, "ok": bool(ok), "severity": severity,
            "required": severity == BLOCKING,
            "detail": detail, "message": detail,
            "fix": fix if not ok else "",
            "auto_fixable": bool(auto and not ok)}


# ---------------------------------------------------------------- contrôles
def _check_llm(core) -> dict[str, Any]:
    """Un modèle doit être joignable, sinon JARVIS ne raisonne pas du tout."""
    configured = str(core.settings.get("ai", "default_model", "") or "").strip()
    online: list[str] = []
    try:
        for status in core.llm.status(max_age=0, blocking=True):
            if status.get("connected"):
                online.extend(status.get("models") or [])
    except Exception as exc:
        return _check("llm", "Modèle de langage", False, severity=BLOCKING,
                      detail=f"Sonde impossible : {exc}",
                      fix="Démarre Ollama (ollama serve) ou ajoute une clé API "
                          "dans Réglages → IA.")
    if online:
        return _check("llm", "Modèle de langage", True, severity=BLOCKING,
                      detail=f"{len(online)} modèle(s) disponible(s)"
                             + (f", défaut « {configured} »" if configured
                                else " — aucun modèle par défaut choisi"))
    ollama = _port_open("127.0.0.1", 11434)
    return _check("llm", "Modèle de langage", False, severity=BLOCKING,
                  detail=("Ollama répond mais n'a aucun modèle." if ollama
                          else "Aucun fournisseur joignable."),
                  fix=("ollama pull qwen2.5:7b" if ollama else
                       "Installe Ollama (ollama.com) puis « ollama pull qwen2.5:7b », "
                       "ou renseigne une clé API dans Réglages → IA."))


def _check_blender(core) -> dict[str, Any]:
    try:
        detection = core.blender.detect()
    except Exception as exc:
        return _check("blender", "Blender", False, detail=f"Détection impossible : {exc}",
                      fix="Installe Blender puis renseigne Réglages → Blender → "
                          "executable_path.")
    if detection.get("installed"):
        return _check("blender", "Blender", True,
                      detail=f"{detection.get('version', '?')} — "
                             f"{detection.get('executable_path', '')}")
    return _check("blender", "Blender", False,
                  detail="Aucune installation trouvée (PATH, /Applications).",
                  fix="Installe Blender depuis blender.org, ou renseigne le chemin "
                      "exact dans Réglages → Blender → executable_path.")


def _check_comfyui(core) -> dict[str, Any]:
    from .comfyui_detect import detect_comfy_image_engines

    base = str(core.settings.get("image", "comfy_url", "") or "http://127.0.0.1:8188")
    try:
        result = detect_comfy_image_engines(base)
    except Exception as exc:
        return _check("comfyui", "ComfyUI", False, detail=str(exc),
                      fix="Lance ComfyUI sur 127.0.0.1:8188.")
    if not result.get("reachable"):
        return _check("comfyui", "ComfyUI", False, severity=DEGRADED,
                      detail=f"Injoignable sur {base} — la génération d'image "
                             "haute qualité / avatar est indisponible.",
                      fix="Démarre ComfyUI (launcher macOS, cf. docs) et vérifie le port 8188.")
    engines = result.get("engines") or []
    if not engines:
        return _check("comfyui", "ComfyUI", False,
                      detail="En ligne, mais aucun checkpoint exploitable détecté.",
                      fix="Place un checkpoint SDXL dans ComfyUI/models/checkpoints.")
    return _check("comfyui", "ComfyUI", True,
                  detail=f"{len(engines)} moteur(s) : "
                         + ", ".join(str(e.get("id", "?")) for e in engines[:4]))


def _check_piper(core) -> dict[str, Any]:
    engine = core.tts.engine_status()
    # Le repli Edge ne rend pas le contrôle vert : il dépanne la voix, mais il
    # n'est pas local. Il est seulement signalé pour éviter de croire JARVIS muet.
    fallback = (" Repli edge-tts actif (voix non locale)."
                if core.tts_fallback.available() else "")
    if not engine.get("available"):
        return _check("piper", "Voix locale (Piper)", False,
                      detail="Moteur Piper absent." + fallback,
                      fix="pip install piper-tts", auto=True)
    if not core.tts.installed():
        return _check("piper", "Voix locale (Piper)", False,
                      detail="Moteur présent, aucune voix française installée." + fallback,
                      fix="python -m jarvis.tts install", auto=True)
    return _check("piper", "Voix locale (Piper)", True, detail=core.tts.summary())


def _check_stt(core) -> dict[str, Any]:
    status = core.stt.status()
    provider = str(core.settings.get("voice", "stt_provider", "browser") or "browser")
    if status.get("available"):
        cached = "" if status.get("model_cached") else " (poids non encore téléchargés)"
        return _check("stt", "Dictée serveur", True,
                      detail=f"faster-whisper, modèle « {status.get('model')} »{cached}")
    return _check("stt", "Dictée serveur", False,
                  severity=BLOCKING if provider == "local" else OPTIONAL,
                  detail="Sans elle, la dictée dépend de la Web Speech API "
                         "(absente de Firefox, distante dans Chrome).",
                  fix="pip install faster-whisper", auto=True)


def _check_playwright(core) -> dict[str, Any]:
    if not _module("playwright"):
        return _check("playwright", "Automatisation navigateur", False,
                      detail="Pilotage web, rendu PDF des factures et du CRM indisponibles.",
                      fix="pip install playwright && python -m playwright install chromium",
                      auto=True)
    # Le paquet ne suffit pas : le navigateur doit aussi avoir été téléchargé.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        missing = "not installed" in (proc.stdout + proc.stderr).lower()
    except Exception:
        missing = False
    if missing:
        return _check("playwright", "Automatisation navigateur", False,
                      detail="Paquet présent, navigateur Chromium non téléchargé.",
                      fix="python -m playwright install chromium", auto=True)
    return _check("playwright", "Automatisation navigateur", True,
                  detail="playwright + chromium")


def _check_discord(core) -> dict[str, Any]:
    if _module("discord"):
        return _check("discord", "Bot Discord", True, detail="discord.py présent")
    return _check("discord", "Bot Discord", False, severity=OPTIONAL,
                  detail="Bot et notifications blog→Discord inactifs.",
                  fix="pip install discord.py", auto=True)


def _check_vault(core) -> dict[str, Any]:
    if not _module("cryptography"):
        return _check("vault", "Coffre à secrets", False, severity=BLOCKING,
                      detail="Sans « cryptography », les secrets chiffrés ne se déchiffrent pas.",
                      fix="pip install cryptography", auto=True)
    try:
        from .secrets import MasterKey

        source = MasterKey().source
    except Exception as exc:
        return _check("vault", "Coffre à secrets", False, severity=BLOCKING,
                      detail=f"Clé maîtresse illisible : {exc}",
                      fix="Vérifie JARVIS_MASTER_KEY dans .env, ou le trousseau "
                          "macOS (Keychain).")
    return _check("vault", "Coffre à secrets", True, detail=f"clé maîtresse : {source}")


def _check_gpu(core) -> dict[str, Any]:
    from .gpu_manager import GpuResourceManager

    vram = GpuResourceManager.vram()
    if not vram:
        return _check("gpu", "Mesure VRAM", False, severity=OPTIONAL,
                      detail="Aucune sonde n'a répondu : l'arbitrage VRAM entre Ollama "
                             "et Blender est désactivé (aucune décision inventée).",
                      fix="Les compteurs de performance macOS (ioreg) doivent être actifs.")
    return _check("gpu", "Mesure VRAM", True,
                  detail=f"{vram.get('vendor', '?')} — {vram['free_mb']} / "
                         f"{vram['total_mb']} Mo libres (source : {vram.get('source', '?')})")


def _check_supervisor(core) -> dict[str, Any]:
    from .self_upgrade.service import DEFAULT_SUPERVISOR_URL
    from .self_upgrade.supervisor_client import SupervisorClient

    url = DEFAULT_SUPERVISOR_URL
    try:
        url = str(core.self_upgrade.config().get("supervisor_url", url) or url)
    except Exception:
        pass
    if SupervisorClient(url).ping():
        return _check("supervisor", "Supervisor (auto-mise à jour)", True,
                      detail=f"en ligne — {url}")
    return _check("supervisor", "Supervisor (auto-mise à jour)", False,
                  detail=f"Injoignable sur {url} : une mise à jour ne pourrait pas "
                         "être annulée.",
                  fix="Lance run_supervisor.command (service séparé, par conception).")


def _check_clap(core) -> dict[str, Any]:
    enabled = bool(core.settings.get("voice", "clap_enabled", False))
    missing = [name for name in ("sounddevice", "numpy") if not _module(name)]
    if missing:
        return _check("clap", "Réveil au double clap", False, severity=OPTIONAL,
                      detail="Dépendances audio absentes : " + ", ".join(missing),
                      fix="pip install -r requirements-audio.txt", auto=True)
    return _check("clap", "Réveil au double clap", True,
                  detail="dépendances présentes"
                         + ("" if enabled else ", désactivé dans les réglages"))


def _check_n8n(core) -> dict[str, Any]:
    try:
        rows = [c for c in core.connectors.list() if c.get("type") == "n8n"]
    except Exception as exc:
        return _check("n8n", "Connecteur n8n", False, severity=OPTIONAL,
                      detail=f"Liste des connecteurs illisible : {exc}",
                      fix="Vérifie la base de données locale (data/jarvis.db) "
                          "et Réglages → Connecteurs.")
    if not rows:
        return _check("n8n", "Connecteur n8n", False, severity=OPTIONAL,
                      detail="Aucune instance n8n déclarée : aucune automatisation "
                             "externe branchée.",
                      fix="Réglages → Connecteurs → n8n : renseigne l'URL de l'instance.")
    return _check("n8n", "Connecteur n8n", True, detail=f"{len(rows)} instance(s) déclarée(s)")


def _check_vision(core) -> dict[str, Any]:
    """L'évaluation du rendu avatar exige un modèle multimodal réellement branché."""
    models: list[str] = []
    try:
        for status in core.llm.status(max_age=30):
            if status.get("connected"):
                models.extend(status.get("models") or [])
    except Exception:
        models = []
    hints = ("llava", "vision", "-vl", "minicpm", "moondream", "gemma3", "pixtral")
    found = [m for m in models if any(h in str(m).lower() for h in hints)]
    if found:
        return _check("vision", "Évaluation visuelle (avatar)", True,
                      detail="modèle(s) vision : " + ", ".join(found[:3]))
    return _check("vision", "Évaluation visuelle (avatar)", False, severity=OPTIONAL,
                  detail="Sans modèle de vision, le rendu avatar ne peut pas être noté "
                         "automatiquement.",
                  fix="ollama pull llava:13b (ou un autre modèle multimodal).")


CHECKS: tuple[tuple[str, Callable[[Any], dict[str, Any]]], ...] = (
    ("llm", _check_llm),
    ("vault", _check_vault),
    ("blender", _check_blender),
    ("comfyui", _check_comfyui),
    ("piper", _check_piper),
    ("stt", _check_stt),
    ("playwright", _check_playwright),
    ("gpu", _check_gpu),
    ("supervisor", _check_supervisor),
    ("discord", _check_discord),
    ("clap", _check_clap),
    ("n8n", _check_n8n),
    ("vision", _check_vision),
)


# Contrôles sans accès réseau ni sous-processus : utilisables dans la bannière
# de démarrage, où l'on ne peut pas se permettre d'attendre.
FAST_CHECKS = ("vault", "stt", "discord", "clap", "gpu")

# Sondes qui partagent l'état mutable du manager LLM : toujours séquentielles.
_LLM_CORE_CHECKS = ("llm", "vision")


def diagnose(core, only: list[str] | None = None) -> dict[str, Any]:
    """État réel de chaque brique. Ne modifie rien, ne lève jamais."""
    wanted = set(only or [])

    def probe_one(name: str, fn: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fn(core)
        except Exception as exc:  # une sonde cassée ne doit pas masquer les autres
            return _check(name, name, False, detail=f"Sonde en échec : {exc}")

    jobs = [(name, fn) for name, fn in CHECKS if (not wanted or name in wanted)]
    # Les sondes partageant de l'état mutable (manager LLM) restent
    # séquentielles ; les autres (sous-processus, réseau) s'exécutent en
    # parallèle : les timeouts d'un service mort ne s'additionnent pas.
    serial = [j for j in jobs if j[0] in _LLM_CORE_CHECKS]
    parallel = [j for j in jobs if j[0] not in serial]
    by_name: dict[str, dict[str, Any]] = {}
    if parallel:
        with ThreadPoolExecutor(max_workers=min(4, len(parallel))) as pool:
            futures = {pool.submit(probe_one, n, fn): n for n, fn in parallel}
            for future in as_completed(futures):
                by_name[futures[future]] = future.result()
    for name, fn in serial:
        by_name[name] = probe_one(name, fn)
    results = [by_name[name] for name, _ in jobs]

    failing = [r for r in results if not r["ok"]]
    return {
        "checks": results,
        "ok": not failing,
        "blocking": [r["name"] for r in failing if r["severity"] == BLOCKING],
        "auto_fixable": [r["name"] for r in failing if r["auto_fixable"]],
        "summary": f"{len(results) - len(failing)}/{len(results)} briques opérationnelles",
        "data_dir": str(DATA_DIR),
    }


# =========================================================================
# DOCTOR SYSTÈME — « pourquoi JARVIS ne démarre-t-il pas ? »
#
# Règles de cette section :
#   * aucun Core requis : chaque contrôle marche même si le Core est cassé ;
#   * AUCUNE écriture : la base est ouverte en lecture stricte (mode=ro),
#     aucun répertoire n'est créé, aucun processus n'est tué ni démarré ;
#   * l'erreur exacte est restituée (type, message, fichier, ligne) ;
#   * les chantiers protégés (Brainrot, Mission Control) sont diagnostiqués
#     en LECTURE SEULE — le Doctor signale, il ne répare pas.
# =========================================================================

# Imports dont l'échec empêche JARVIS de démarrer ou de raisonner.
CRITICAL_IMPORTS = (
    "jarvis.config", "jarvis.events", "jarvis.db", "jarvis.core",
    "jarvis.server", "jarvis.orchestrator", "jarvis.tasks",
    "jarvis.llm.manager", "jarvis.tools.runner",
    "jarvis.mission_control", "jarvis.mission_store",
)

# Imports Brainrot : LECTURE SEULE, jamais réparés par le Doctor.
BRAINROT_IMPORTS = (
    "jarvis.brainrot_operator",
    "jarvis.brainrot_capability_discovery",
    "jarvis.tools.brainrot_member_tools",
    "jarvis.tools.brainrot_middleware",
    "jarvis.tools.brainrot_brainrot_tools",
    "jarvis.tools.brainrot_wiki_tools",
    "jarvis.tools.brainrot_forum_tools",
)

_MISSION_FILES = (
    "ui/js/v5/spatial_mission_control.js",
    "ui/css/v5/mission_control.css",
    "tests/frontend/harness.js",
)
_REQUIRED_DB_TABLES = ("settings", "tasks", "missions", "mission_events", "connectors")

# Clés d'environnement RÉELLEMENT lues par le projet (connectors.bootstrap_from_env,
# secrets.MasterKey). Les VALEURS ne sont jamais lues ni affichées — seulement
# leur présence.
_ENV_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
             "GROQ_API_KEY", "OPENROUTER_API_KEY", "JARVIS_MASTER_KEY")


# Sonde d'imports : un interpréteur NEUF, exactement comme au démarrage de
# JARVIS. Un import qui échoue est restitué avec type, message, fichier, ligne.
_IMPORT_PROBE_CODE = r'''
import importlib, json, sys, traceback
out = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
        out[name] = {"ok": True}
    except Exception as exc:
        tb = traceback.extract_tb(sys.exc_info()[2])
        frame = next((f for f in reversed(tb) if f.filename), None)
        out[name] = {"ok": False, "error": type(exc).__name__, "message": str(exc),
                     "file": frame.filename if frame else "", "line": frame.lineno if frame else 0}
print(json.dumps(out))
'''

# Sonde du registre d'outils : interpréteur neuf + espion posé sur
# `registry.register` AVANT que jarvis/tools/__init__.py enregistre quoi que ce
# soit — c'est le seul moyen de voir un doublon d'ID (l'écrasement est muet).
_TOOL_PROBE_CODE = r'''
import builtins, json, sys
registered = []
real_import = builtins.__import__
def _spy(name, *args, **kwargs):
    mod = real_import(name, *args, **kwargs)
    base = sys.modules.get("jarvis.tools.base")
    # `base` peut être partiellement initialisé (import circulaire) : à ce
    # moment son attribut `registry` n'existe pas encore ; on repassera quand
    # l'import aura fini. C'est le seul point d'accroche avant les enregistrements.
    reg = getattr(base, "registry", None) if base is not None else None
    if reg is not None and not getattr(reg, "_doctor_spy", False):
        reg._doctor_spy = True
        orig = reg.register
        def register(tool, _orig=orig):
            registered.append(tool.id)
            return _orig(tool)
        reg.register = register
    return mod
builtins.__import__ = _spy
import jarvis.tools  # noqa: F401  (l'import lui-même enregistre les outils)
from jarvis.tools.base import registry
tools = [{"id": t.id, "category": t.category, "risk": t.risk,
          "enabled": bool(t.enabled),
          "handler": callable(getattr(t, "handler", None))} for t in registry.all()]
dups = sorted({i for i in registered if registered.count(i) > 1})
print(json.dumps({"total": len(tools), "registered": len(registered),
                  "duplicates": dups,
                  "no_handler": [t["id"] for t in tools if not t["handler"]],
                  "disabled": [t["id"] for t in tools if not t["enabled"]],
                  "risks": {r: sum(1 for t in tools if t["risk"] == r) for r in sorted({t["risk"] for t in tools})},
                  "categories": registry.categories()}))
'''


def _run_probe(code: str, *argv: str, timeout: float = 120.0) -> dict[str, Any]:
    """Exécute une sonde dans un interpréteur neuf ; jamais d'exception."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, *argv], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() \
            else {"probe_error": (proc.stderr or "aucune sortie").strip()[-400:]}
    except Exception as exc:
        return {"probe_error": f"sonde impossible : {exc}"}


def probe_imports(names: tuple[str, ...]) -> dict[str, Any]:
    """État d'import de chaque module, dans un interpréteur vierge."""
    result = _run_probe(_IMPORT_PROBE_CODE, json.dumps(list(names)))
    if "probe_error" in result:
        return {name: {"ok": False, "error": "DoctorProbe",
                       "message": result["probe_error"], "file": "", "line": 0}
                for name in names}
    return result


def probe_tools() -> dict[str, Any]:
    """Inventaire réel du registre d'outils (interpréteur neuf, sans écriture)."""
    return _run_probe(_TOOL_PROBE_CODE)


def _sqlite_ro(db_path: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Lecture stricte : `mode=ro` — impossible d'écrire, même par accident."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    try:
        cur = con.execute(sql, params)
        cols = [c[0] for c in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _http_json(url: str, timeout: float = NET_TIMEOUT_S) -> Any:
    """GET court. Lève en cas d'erreur réseau ou de décodage."""
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8", "replace"))


def _port_state(port: int, timeout: float = 1.0) -> dict[str, Any]:
    """FREE / LISTENING + propriétaire (PID, nom) si possible. Ne tue rien."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except Exception:
        return {"port": port, "state": "free"}
    info: dict[str, Any] = {"port": port, "state": "listening", "pid": None, "name": ""}
    if IS_WINDOWS:
        return _port_state_windows(info, port, timeout)
    return _port_state_macos(info, port)


def _port_state_windows(info: dict[str, Any], port: int, timeout: float) -> dict[str, Any]:
    """Nom/PID du processus qui écoute sur `port` (Windows : netstat + tasklist)."""
    try:
        proc = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                    and parts[1].rsplit(":", 1)[-1] == str(port):
                info["pid"] = int(parts[4])
                break
    except Exception:
        return info
    if info["pid"]:
        try:
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {info['pid']}", "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=5,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            first = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
            info["name"] = first.split('","')[0].strip('"') if '","' in first else ""
        except Exception:
            pass
    return info


def _port_state_macos(info: dict[str, Any], port: int) -> dict[str, Any]:
    """Nom/PID du processus qui écoute sur `port` (macOS : lsof)."""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return info
    header = True
    for line in proc.stdout.splitlines():
        if header:
            header = False
            continue
        parts = line.split()
        if len(parts) >= 2:
            info["name"] = parts[0]
            if parts[1].isdigit():
                info["pid"] = int(parts[1])
            break
    return info


def _llm_endpoints() -> list[tuple[str, str]]:
    """Endpoints réellement configurés + le serveur local usuel, sans doublon."""
    endpoints: list[tuple[str, str]] = [("openai", DEFAULT_LOCAL_LLM_URL)]
    try:
        for row in _sqlite_ro(DB_PATH, "SELECT type, config FROM connectors WHERE enabled=1"):
            try:
                cfg = json.loads(row.get("config") or "{}")
            except Exception:
                continue
            base = str(cfg.get("base_url") or "").strip()
            if base:
                endpoints.append((str(row.get("type") or ""), base))
    except Exception:
        pass  # base absente/illisible : il ne reste que l'endpoint local usuel
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for ctype, base in endpoints:
        key = base.rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append((ctype, base))
    return unique


def probe_llm(endpoints: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Sonde chaque endpoint et retourne les Model IDs RÉELS, jamais codés en dur."""
    results: list[dict[str, Any]] = []
    for ctype, base in endpoints:
        base = base.rstrip("/")
        if ctype == "ollama":
            models_url = f"{base}/api/tags"
        else:
            models_url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        probe: dict[str, Any] = {"type": ctype, "url": models_url,
                                "status": "offline", "models": [], "error": ""}
        try:
            payload = _http_json(models_url)
        except Exception as exc:
            probe["error"] = f"{type(exc).__name__}: {exc}".strip()[:200]
            results.append(probe)
            continue
        if ctype == "ollama":
            models = [str(m.get("name")) for m in payload.get("models") or [] if m.get("name")]
        else:
            data = payload.get("data") if isinstance(payload, dict) else None
            models = [str(m.get("id")) for m in data or [] if isinstance(m, dict) and m.get("id")]
        probe["models"] = models
        probe["status"] = "online" if models else "degraded"
        if not models:
            probe["error"] = "endpoint joignable mais aucun modèle listé"
        results.append(probe)
    return results


def _sys_result(name: str, status: str, detail: str = "", *,
                recommendation: str = "", blocking: bool | None = None,
                severity: str | None = None,
                details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Résultat d'un contrôle système.

    `severity` dit à quelle classe de dépendance appartient le contrôle :
    REQUIRED (BLOCKING) — sans lui le fonctionnement principal s'arrête ;
    DEGRADED — JARVIS démarre mais une capacité importante manque ;
    OPTIONAL — fonction spécialisée indisponible, sans impact général.
    `required` = la classe REQUIRED. `blocking` (code de sortie 1) découle de
    `severity == BLOCKING` ET statut ERROR : un contrôle REQUIRED en échec.
    """
    if severity is None:
        severity = BLOCKING if blocking else DEGRADED
    is_blocking = severity == BLOCKING and status == ERROR
    if blocking and not is_blocking:
        is_blocking = True
    return {"name": name, "label": SYSTEM_LABELS.get(name, name), "status": status,
            "detail": detail, "message": detail,
            "recommendation": recommendation if status != OK else "",
            "severity": severity, "required": severity == BLOCKING,
            "blocking": bool(is_blocking), "details": details or {}}


def _sys_check_python() -> dict[str, Any]:
    version = ".".join(str(p) for p in sys.version_info[:3])
    arch = f"{platform.machine()} — {'Windows' if IS_WINDOWS else platform.system()}"
    venv = sys.prefix != sys.base_prefix
    suffix = f" · venv : {sys.prefix}" if venv else " · pas d'environnement virtuel"
    if sys.version_info < (3, 10):
        return _sys_result("python", ERROR, f"{version} — trop ancien ({arch}){suffix}",
                           recommendation="Installe Python 3.10 ou plus (le code utilise "
                                         "les unions `X | Y` en annotation).",
                           blocking=True)
    return _sys_result("python", OK, f"{version} ({arch}){suffix}")


def _sys_check_repository() -> dict[str, Any]:
    essentials = ("jarvis/__init__.py", "jarvis/core.py", "jarvis/server.py",
                 "jarvis/config.py", "jarvis.py", "ui/index.html")
    missing = [f for f in essentials if not (ROOT / f).is_file()]
    if missing:
        return _sys_result("repository", ERROR, "fichiers essentiels absents : " + ", ".join(missing),
                           recommendation="Le dépôt semble incomplet : git clone / git checkout "
                                         "depuis la racine attendue.", blocking=True)
    problems: list[str] = []
    for d in (DATA_DIR, LOG_DIR, BACKUP_DIR):
        if d.exists():
            if not os.access(d, os.W_OK):
                problems.append(f"{d} non inscriptible")
        else:
            parent = d.parent
            if not (parent.exists() and os.access(parent, os.W_OK)):
                problems.append(f"{d} absent et non créable")
            else:
                problems.append("")  # absent mais créable : normal avant 1er démarrage
    blocking_writes = [p for p in problems if p]
    detail = f"{ROOT}"
    if blocking_writes:
        return _sys_result("repository", ERROR, f"droits d'écriture manquants : {', '.join(blocking_writes)}",
                           recommendation="Vérifie les permissions sur les dossiers runtime "
                                         "(data, logs, backups).", blocking=True,
                           details={"root": str(ROOT), "missing_files": missing})
    return _sys_result("repository", OK, detail,
                       details={"root": str(ROOT), "missing_files": missing,
                                "runtime_dirs_ok": [str(d) for d in (DATA_DIR, LOG_DIR, BACKUP_DIR)]})


def _sys_check_config(rows: int | None = 0) -> dict[str, Any]:
    """`rows` = nombre de réglages persistés (None si base illisible)."""
    from .config import DEFAULT_SETTINGS  # import réel : il échoue si la config est cassée

    if rows is None:
        return _sys_result("configuration", WARNING,
                           f"réglages par défaut chargés ({len(DEFAULT_SETTINGS)} sections) — "
                           "base de réglages illisible",
                           recommendation="Voir le contrôle Database : la base sera recréée si absente.")
    if rows == 0:
        return _sys_result("configuration", OK,
                           f"réglages par défaut chargés ({len(DEFAULT_SETTINGS)} sections), "
                           "aucune personnalisation persistée")
    return _sys_result("configuration", OK,
                       f"réglages par défaut ({len(DEFAULT_SETTINGS)} sections) + {rows} entrée(s) persistée(s)")


def _sys_check_secrets(env: dict[str, str] | None = None,
                        vault_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Présence des secrets — JAMAIS leur valeur (rien n'est lu ici)."""
    env = os.environ if env is None else env
    lines: list[str] = []
    details: dict[str, Any] = {"env": {}}
    for key in _ENV_KEYS:
        present = bool(str(env.get(key, "")).strip())  # présence, jamais la valeur
        lines.append(f"{key:<20} {'SET' if present else 'MISSING'}")
        details["env"][key] = "set" if present else "missing"
    if vault_rows is None:
        try:
            vault_rows = _sqlite_ro(DB_PATH, "SELECT field, COUNT(*) AS n FROM secrets GROUP BY field")
        except Exception:
            vault_rows = []
    total = 0
    if vault_rows:
        details["vault"] = {str(r.get("field")): int(r.get("n", 0)) for r in vault_rows}
        total = sum(int(r.get("n", 0)) for r in vault_rows)
        lines.append(f"{'Vault (champs)':<20} " + ", ".join(
            f"{r.get('field')}×{r.get('n')}" for r in vault_rows))
    else:
        lines.append(f"{'Vault (champs)':<20} EMPTY")
    any_key = sum(1 for v in details["env"].values() if v == "set") or total
    if not any_key:
        return _sys_result("secrets", WARNING, "\n".join(lines),
                           recommendation="Aucune clé ni secret configuré : JARVIS démarrera sans "
                                         "fournisseur IA distant. Renseigne une clé API "
                                         "(variables listées) ou un connecteur local.",
                           details=details)
    return _sys_result("secrets", OK, "\n".join(lines), details=details)


def _sys_check_database(db_path: Any = None) -> dict[str, Any]:
    path = str(DB_PATH if db_path is None else db_path)
    if not os.path.exists(path):
        return _sys_result("database", WARNING, f"{path} — absente",
                           recommendation="Elle sera créée au premier démarrage de JARVIS. "
                                         "Ce n'est pas bloquant : le direct continue sans base.",
                           details={"path": path, "exists": False})
    try:
        tables = {r.get("name") for r in _sqlite_ro(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception as exc:
        return _sys_result("database", ERROR, f"{path} — illisible : {type(exc).__name__}: {exc}",
                           recommendation="Base corrompue : le Core ne démarrera pas. Restaure "
                                         "la dernière copie de data/backups/.", blocking=True,
                           details={"path": path, "error": str(exc)})
    missing = [t for t in _REQUIRED_DB_TABLES if t not in tables]
    version = "?"
    try:
        rows = _sqlite_ro(path, "SELECT value FROM meta WHERE key='schema_version'")
        version = str(rows[0]["value"]) if rows else "?"
    except Exception:
        pass
    try:
        n_settings = len(_sqlite_ro(path, "SELECT 1 FROM settings"))
    except Exception:
        n_settings = 0
    if missing:
        return _sys_result("database", WARNING,
                           f"{path} — tables absentes : {', '.join(missing)}",
                           recommendation="Tables créées au premier démarrage ; si elles manquent "
                                         "alors que JARVIS a déjà tourné, la migration n'a pas fini.",
                           details={"path": path, "missing_tables": missing, "schema_version": version})
    return _sys_result("database", OK,
                       f"SQLite — schéma v{version}, {len(tables)} tables, {n_settings} réglages (lecture seule)",
                       details={"path": path, "schema_version": version, "tables": sorted(tables)})


def _sys_check_eventbus() -> dict[str, Any]:
    """Bus isolé, événement de test namespaced : la vraie activité n'est pas polluée."""
    try:
        from .events import EventBus

        bus = EventBus(None)          # aucune base : l'événement de test n'est jamais persisté
        sid, q = bus.subscribe()
        received: list[dict[str, Any]] = []
        bus.on("doctor.selftest", received.append)
        bus.emit("doctor.selftest", {"probe": True})   # persist=False par défaut
        event = q.get(timeout=1.0)
        bus.unsubscribe(sid)
        if not received or not event or event["type"] != "doctor.selftest":
            raise AssertionError("l'événement de test n'est pas revenu")
    except Exception as exc:
        return _sys_result("eventbus", ERROR, f"{type(exc).__name__}: {exc}",
                           recommendation="Vérifie jarvis/events.py — le bus est la colonne "
                                         "vertébrale de l'interface temps réel.", blocking=True)
    return _sys_result("eventbus", OK, "création, abonnement, publication et réception vérifiés (bus isolé)")


def _sys_check_imports(probe: dict[str, Any]) -> dict[str, Any]:
    broken = {name: info for name, info in probe.items() if not info.get("ok")}
    if broken:
        lines = [f"{name} — {info.get('error')}: {info.get('message')}"
                 f" ({info.get('file')}:{info.get('line')})"
                 for name, info in broken.items()]
        return _sys_result("imports", ERROR,
                           f"{len(broken)}/{len(CRITICAL_IMPORTS)} imports cassés — " + " · ".join(lines),
                           recommendation="Corrige l'import signalé (dépendance manquante, "
                                         "syntaxe, module renommé) : JARVIS ne démarre pas tant "
                                         "qu'il échoue.", blocking=True,
                           details={"broken": broken})
    return _sys_result("imports", OK, f"{len(CRITICAL_IMPORTS)} modules critiques importés sans erreur")


def _llm_provider_status(core: Any) -> list[dict[str, Any]] | None:
    """État réel de TOUS les fournisseurs (LLMManager), ou None si indisponible.

    C'est la source de vérité pour ne pas déclarer JARVIS bloqué à cause d'un
    serveur local éteint alors qu'un autre provider configuré répond.
    """
    try:
        raw = core.llm.status(max_age=0, blocking=True)
        out = list(raw)
        if out and all(isinstance(p, dict) for p in out):
            return out
    except Exception:
        pass
    return None


def _provider_configured(status: dict[str, Any]) -> bool:
    """Un fournisseur réellement configuré (les placeholders `__type` non)."""
    return bool(str(status.get("id", "")).strip())


def _sys_check_llm_server(probes: list[dict[str, Any]],
                          provider_status: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Endpoint joignable + un provider opérationnel ? (REQUIRED : sans LLM,
    l'orchestrateur ne peut ni raisonner ni répondre.)"""
    if provider_status is not None:
        connected = [p for p in provider_status if p.get("connected")
                     and (p.get("models") or [])]
        if connected:
            names = ", ".join(f"{p.get('type', '?')} ({p.get('name', '?')})"
                              for p in connected[:4])
            return _sys_result("llm_server", OK,
                               f"{len(connected)} fournisseur(s) connecté(s) — {names}",
                               severity=BLOCKING,
                               details={"providers": [
                                   {"id": p.get("id"), "type": p.get("type"),
                                    "name": p.get("name"), "models": (p.get("models") or [])[:6]}
                                   for p in connected]})
        configured = [p for p in provider_status if _provider_configured(p)]
        if not configured:
            return _sys_result(
                "llm_server", ERROR, "aucun fournisseur configuré "
                                     "(ni clé API ni serveur local joignable)",
                recommendation="Renseigne une clé API (Réglages → IA) ou démarre un "
                              "serveur local OpenAI-compatible sur 127.0.0.1:8080.",
                severity=BLOCKING, details={"providers": provider_status})
        blobs = ", ".join(f"{p.get('type', '?')}: {p.get('detail', '')}"
                          for p in configured)
        return _sys_result("llm_server", ERROR,
                           "aucun fournisseur joignable — " + blobs,
                           recommendation="Démarre le serveur local (llama.cpp sur "
                                         "127.0.0.1:8080, ou Ollama) / vérifie la "
                                         "clé API du provider distant.",
                           severity=BLOCKING, details={"providers": provider_status})
    online = [p for p in probes if p.get("status") == "online"]
    degraded = [p for p in probes if p.get("status") == "degraded"]
    if online:
        return _sys_result("llm_server", OK,
                           f"{len(online)} endpoint(s) en ligne — "
                           + ", ".join(p["url"] for p in online),
                           severity=BLOCKING)
    if degraded:
        return _sys_result("llm_server", WARNING,
                           "joignable mais aucun modèle listé : "
                           + ", ".join(p["url"] for p in degraded),
                           recommendation="Le serveur répond mais ne liste aucun modèle : "
                                         "vérifie le chargement du modèle côté serveur.",
                           severity=BLOCKING,
                           details={"probes": probes})
    return _sys_result("llm_server", ERROR,
                       "aucun endpoint joignable : "
                       + ", ".join(f"{p['url']} ({p['error']})" for p in probes) or "aucun endpoint configuré",
                       recommendation="Vérifie que le serveur llama.cpp écoute sur 127.0.0.1:8080 "
                                     "(ou Ollama sur 11434). Le Doctor ne démarre rien "
                                     "automatiquement.",
                       blocking=True,
                       details={"probes": [{k: p.get(k) for k in ("url", "status", "error")}
                                            for p in probes]})


def _sys_check_llm_model(probes: list[dict[str, Any]],
                         provider_status: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    models: list[str] = []
    source = ""
    if provider_status is not None:
        connected = [p for p in provider_status if p.get("connected")
                     and (p.get("models") or [])]
        if connected:
            models = sorted({str(m) for p in connected for m in (p.get("models") or [])})
            source = ", ".join(f"{p.get('type', '?')} ({p.get('name', '?')})"
                               for p in connected[:4])
        elif not [p for p in provider_status if _provider_configured(p)]:
            return _sys_result("llm_model", ERROR,
                               "aucun modèle disponible — aucun fournisseur configuré",
                               recommendation="Renseigne une clé API ou démarre un serveur "
                                             "local : sans modèle, JARVIS ne raisonne pas.",
                               severity=BLOCKING, details={"providers": provider_status})
        else:
            return _sys_result("llm_model", WARNING,
                               "aucun modèle disponible — voir LLM Server "
                               "(relié aux mêmes fournisseurs)",
                               severity=BLOCKING, details={"providers": provider_status})
        if not models:
            return _sys_result("llm_model", ERROR, "aucun modèle disponible",
                               recommendation="Charge un modèle sur le serveur : "
                                             "sans modèle, JARVIS ne raisonne pas.",
                               severity=BLOCKING)
        return _sys_result("llm_model", OK,
                           f"{len(models)} modèle(s) sur {source} : "
                           + ", ".join(models[:6]) + (" …" if len(models) > 6 else ""),
                           severity=BLOCKING,
                           details={"models": models, "source": source})
    for p in probes:
        if p.get("status") == "online" and p.get("models"):
            models = [str(m) for m in p["models"]]
            source = p["url"]
            break
    if not models:
        offline = not any(p.get("status") != "offline" for p in probes)
        if offline:
            return _sys_result("llm_model", WARNING,
                               "aucun modèle disponible — dépend du serveur LLM (voir LLM Server)")
        return _sys_result("llm_model", ERROR, "serveur joignable mais aucun modèle disponible",
                           recommendation="Charge un modèle sur le serveur (llama.cpp -m … ou "
                                         "ollama pull …) : sans modèle, JARVIS ne raisonne pas.")
    return _sys_result("llm_model", OK, f"{len(models)} modèle(s) sur {source} : "
                       + ", ".join(models[:6]) + (" …" if len(models) > 6 else ""),
                       details={"models": models})


def _sys_check_ports(states: dict[int, dict[str, Any]]) -> dict[str, Any]:
    lines: list[str] = []
    problems: list[str] = []
    for port in (LLM_API_PORT, MAIN_API_PORT):
        st = states.get(port, {"state": "free"})
        if st.get("state") == "free":
            lines.append(f"{port} FREE")
        else:
            lines.append(f"{port} LISTENING — {st.get('name') or 'processus inconnu'}"
                         f" (pid {st.get('pid') or '?'})")
            # Le port API appartient à JARVIS : un autre processus qui le tient
            # est exactement la raison pour laquelle JARVIS ne démarre pas.
            if port == MAIN_API_PORT:
                name = (st.get("name") or "").lower()
                if name and not any(k in name for k in ("python", "py", "jarvis")):
                    problems.append(f"8765 tenu par {st.get('name')} (pid {st.get('pid')})")
    if problems:
        return _sys_result("ports", ERROR, " · ".join(lines) + " — " + " ; ".join(problems),
                           recommendation="Libère le port 8765 (ferme le processus indiqué) : "
                                         "le serveur JARVIS ne peut pas s'y lier. Le Doctor ne "
                                         "tue aucun processus.",
                           blocking=True, details={"states": states})
    return _sys_result("ports", OK, " · ".join(lines), details={"states": states})


def _sys_check_api_server(state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = state or _port_state(MAIN_API_PORT)
    if st.get("state") == "free":
        return _sys_result("api_server", OK, f"port {MAIN_API_PORT} libre — JARVIS n'est pas démarré")
    name = (st.get("name") or "").lower()
    if any(k in name for k in ("python", "py", "jarvis")):
        return _sys_result("api_server", OK,
                           f"en ligne sur 127.0.0.1:{MAIN_API_PORT} — {st.get('name')} (pid {st.get('pid')})")
    return _sys_result("api_server", ERROR,
                       f"port {MAIN_API_PORT} occupé par {st.get('name') or 'un processus inattendu'} "
                       f"(pid {st.get('pid') or '?'})",
                       recommendation="UNEXPECTED PROCESS sur le port API : JARVIS ne peut pas "
                                     "démarrer tant qu'il n'est pas libéré.",
                       blocking=True, details={"state": st})


def _sys_check_tools(payload: dict[str, Any]) -> dict[str, Any]:
    if "probe_error" in payload:
        return _sys_result("tool_registry", ERROR, f"sonde impossible : {payload['probe_error']}",
                           recommendation="Vérifie que l'import de jarvis.tools "
                                         "s'exécute sans erreur.", severity=BLOCKING)
    total = int(payload.get("total", 0))
    dups = list(payload.get("duplicates") or [])
    no_handler = list(payload.get("no_handler") or [])
    risks = payload.get("risks") or {}
    if total == 0:
        return _sys_result("tool_registry", ERROR,
                           "registry vide — aucun outil enregistré",
                           recommendation="Aucun outil n'a été importé : vérifie les imports de "
                                         "jarvis/tools/__init__.py.", severity=BLOCKING,
                           details=payload)
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(risks.items()))
    detail = f"TOTAL {total} ({breakdown})"
    if dups:
        detail += f" — DOUBLONS D'ID : {', '.join(dups)}"
    if no_handler:
        detail += f" — handler absent : {', '.join(no_handler[:5])}"
    if dups or no_handler:
        return _sys_result("tool_registry", ERROR, detail,
                           recommendation="Doublon d'ID = un outil en écrase un autre silencieusement ; "
                                         "handler absent = outil non exécutable. Corrige le "
                                         "module d'outils concerné.", severity=BLOCKING,
                           details=payload)
    return _sys_result("tool_registry", OK, detail, severity=BLOCKING, details=payload)


def _sys_check_mission_control(probe: dict[str, Any] | None = None,
                               missing_files: list[str] | None = None,
                               missing_tables: list[str] | None = None) -> dict[str, Any]:
    """LECTURE SEULE : le Doctor ne modifie jamais Mission Control."""
    probe = probe if probe is not None else probe_imports(
        ("jarvis.mission_control", "jarvis.mission_store"))
    broken = {n: i for n, i in probe.items() if not i.get("ok")}
    if broken:
        lines = [f"{n} — {i.get('error')}: {i.get('message')} ({i.get('file')}:{i.get('line')})"
                for n, i in broken.items()]
        return _sys_result("mission_control", ERROR,
                           "imports cassés — " + " · ".join(lines)
                           + " · dépendance bloquante : voir le contrôle Imports",
                           recommendation="Mission Control ne peut pas être testé tant que "
                                         "l'import échoue. Signale le problème — le Doctor ne "
                                         "répare pas ce chantier.",
                           details={"broken": broken})
    if missing_files is None:
        missing_files = [f for f in _MISSION_FILES if not (ROOT / f).is_file()]
    if missing_files:
        return _sys_result("mission_control", ERROR,
                            "fichiers frontend absents : " + ", ".join(missing_files),
                            recommendation="Le panneau Mission Control ne pourra pas se charger. "
                                          "Vérifie le dépôt (git status).",
                            details={"missing_files": missing_files})
    if missing_tables is None:
        try:
            tables = {r.get("name") for r in _sqlite_ro(
                DB_PATH, "SELECT name FROM sqlite_master WHERE type='table'")}
            missing_tables = [t for t in ("missions", "mission_events") if t not in tables]
        except Exception:
            missing_tables = []
    if missing_tables:
        return _sys_result("mission_control", WARNING,
                           "imports OK, fichiers présents, tables absentes : "
                           + ", ".join(missing_tables)
                           + " (créées au premier démarrage ou base absente)",
                           details={"missing_tables": missing_tables})
    return _sys_result("mission_control", OK,
                       "imports OK, fichiers frontend présents, tables missions/mission_events présentes")


def _sys_check_brainrot(probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """Imports Brainrot — LECTURE SEULE, jamais réparés par le Doctor."""
    probe = probe if probe is not None else probe_imports(BRAINROT_IMPORTS)
    broken = {n: i for n, i in probe.items() if not i.get("ok")}
    if broken:
        lines = [f"{n} — {i.get('error')}: {i.get('message')} ({i.get('file')}:{i.get('line')})"
                for n, i in broken.items()]
        return _sys_result("brainrot_imports", WARNING,
                           f"{len(broken)}/{len(BRAINROT_IMPORTS)} imports cassés — "
                           + " · ".join(lines),
                           recommendation="Problème signalé, non corrigé : le chantier Brainrot est "
                                         "protégé (agents dédiés). Le Doctor n'y touche pas.",
                           severity=OPTIONAL,
                           details={"broken": broken})
    return _sys_result("brainrot_imports", OK,
                       f"{len(BRAINROT_IMPORTS)} modules Brainrot importés sans erreur (lecture seule)",
                       severity=OPTIONAL)


def _sys_check_core() -> tuple[dict[str, Any], Any]:
    """Amorce RÉELLE du Core — la reproduction exacte de ce que fait le démarrage.

    Ce contrôle est le seul qui « modifie » quelque chose : il crée les dossiers
    runtime et la base si absents, exactement comme n'importe quel lancement de
    JARVIS. C'est sa raison d'être : dire si oui ou non JARVIS démarre.
    """
    try:
        from .core import JarvisCore

        core = JarvisCore()
    except Exception as exc:
        tb = traceback.extract_tb(sys.exc_info()[2])
        frame = next((f for f in reversed(tb) if f.filename), None)
        where = f" ({frame.filename}:{frame.lineno})" if frame else ""
        return _sys_result("core", ERROR,
                           f"le Core ne démarre pas — {type(exc).__name__}: {exc}{where}",
                           recommendation="Corrige l'erreur ci-dessus : c'est la raison exacte "
                                         "pour laquelle JARVIS ne démarre pas.",
                           blocking=True), None
    return _sys_result("core", OK, "le Core s'amorce : base, bus, réglages et registre initialisés"), core


def _brique_to_result(check: dict[str, Any]) -> dict[str, Any]:
    """Convertit un contrôle « brique » vers le format du doctor système."""
    severity = check.get("severity") or DEGRADED
    if check.get("ok"):
        status = OK
    elif severity == BLOCKING:
        status = ERROR
    else:
        status = WARNING
    return {"name": check.get("name", "?"), "label": check.get("label", check.get("name", "?")),
            "status": status, "detail": check.get("detail", ""),
            "message": check.get("detail", ""),
            "recommendation": check.get("fix", ""),
            "severity": severity, "required": severity == BLOCKING,
            "blocking": bool(status == ERROR and severity == BLOCKING),
            "details": {"severity": check.get("severity"), "auto_fixable": check.get("auto_fixable")}}


def full_diagnose(only: list[str] | None = None) -> dict[str, Any]:
    """Diagnostic complet : système (sans Core) + briques externes (avec Core).

    Ne lève jamais : un contrôle cassé devient un résultat ERROR, pas un crash.
    """
    wanted = set(only or [])

    def keep(name: str) -> bool:
        return not wanted or name in wanted

    checks: list[dict[str, Any]] = []

    def run(name: str, fn: Callable[[], dict[str, Any]]) -> None:
        if not keep(name):
            return
        try:
            checks.append(fn())
        except Exception as exc:  # une sonde cassée ne doit pas masquer les autres
            checks.append(_sys_result(name, ERROR, f"sonde en échec — {type(exc).__name__}: {exc}",
                                      recommendation="Erreur interne du Doctor sur ce contrôle.",
                                      blocking=True))

    # Sondes lentes en parallèle : le Doctor doit rester rapide même face à un
    # service mort (timeout court, jamais plusieurs minutes d'attente). La sonde
    # d'imports couvre déjà les modules Brainrot : pas de second interpréteur
    # rien que pour eux (un interpréteur neuf = plusieurs secondes).
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_imports = pool.submit(probe_imports, CRITICAL_IMPORTS + BRAINROT_IMPORTS)
        fut_llm = pool.submit(probe_llm, _llm_endpoints())
        fut_ports = pool.submit(lambda: {p: _port_state(p) for p in (LLM_API_PORT, MAIN_API_PORT)})
        fut_tools = pool.submit(probe_tools)
        imports_probe = fut_imports.result()
        brainrot_probe = {
            n: imports_probe.get(n, {"ok": False, "error": "DoctorProbe",
                                     "message": "non sondé"})
            for n in BRAINROT_IMPORTS}
        llm_probes = fut_llm.result()
        port_states = fut_ports.result()
        tools_payload = fut_tools.result()

    run("python", _sys_check_python)
    run("repository", _sys_check_repository)
    run("imports", lambda: _sys_check_imports(
        {n: imports_probe.get(n, {"ok": False, "error": "DoctorProbe", "message": "non sondé"})
         for n in CRITICAL_IMPORTS}))
    try:
        rows = len(_sqlite_ro(DB_PATH, "SELECT 1 FROM settings")) if os.path.exists(DB_PATH) else 0
    except Exception:
        rows = None
    run("configuration", lambda: _sys_check_config(rows))
    run("secrets", _sys_check_secrets)
    run("database", _sys_check_database)
    run("eventbus", _sys_check_eventbus)

    # Les contrôles LLM système s'appuient sur l'état réel de TOUS les
    # fournisseurs (LLMManager) : un serveur local 8080 éteint ne déclare pas
    # JARVIS bloqué si un autre provider configuré répond réellement. Pour ça,
    # le Core est amorcé avant ces deux contrôles.
    core = None
    provider_status = None
    want_core = (keep("core") or keep("external_bricks")
                 or any(keep(n) for n, _ in CHECKS)
                 or keep("llm_server") or keep("llm_model"))
    if want_core:
        result, core = _sys_check_core()
        if keep("core"):
            checks.append(result)
        if core is not None and (keep("llm_server") or keep("llm_model")):
            provider_status = _llm_provider_status(core)

    run("llm_server", lambda: _sys_check_llm_server(llm_probes, provider_status))
    run("llm_model", lambda: _sys_check_llm_model(llm_probes, provider_status))
    run("ports", lambda: _sys_check_ports(port_states))
    run("api_server", lambda: _sys_check_api_server(port_states.get(MAIN_API_PORT)))
    run("tool_registry", lambda: _sys_check_tools(tools_payload))
    run("mission_control", lambda: _sys_check_mission_control(
        {n: imports_probe.get(n, {"ok": False, "error": "DoctorProbe", "message": "non sondé"})
         for n in ("jarvis.mission_control", "jarvis.mission_store")}))
    run("brainrot_imports", lambda: _sys_check_brainrot(brainrot_probe))

    if core is None:
        if keep("external_bricks") or any(keep(n) for n, _ in CHECKS):
            checks.append(_sys_result("external_bricks", WARNING,
                                      "contrôles des briques externes sautés : le Core ne démarre pas",
                                      recommendation="Corrige d'abord le contrôle Core startup : "
                                                    "c'est la dépendance bloquante.",
                                      severity=DEGRADED))
    else:
        bricks = diagnose(core, only)
        if bricks["checks"]:
            checks.append({"name": "external_bricks", "label": SYSTEM_LABELS["external_bricks"],
                           "status": OK, "detail": bricks["summary"], "message": bricks["summary"],
                           "recommendation": "", "severity": DEGRADED, "required": False,
                           "blocking": False, "details": {}})
        for check in bricks["checks"]:
            if keep(check["name"]):
                checks.append(_brique_to_result(check))

    blocking = [c["name"] for c in checks if c["blocking"]]
    errors = sum(1 for c in checks if c["status"] == ERROR)
    warnings = sum(1 for c in checks if c["status"] == WARNING)
    degraded = [c["name"] for c in checks
                if c.get("severity") == DEGRADED and c["status"] != OK]
    optional_offline = [c["name"] for c in checks
                        if c.get("severity") == OPTIONAL and c["status"] != OK]
    health = BLOCKED if blocking else (DEGRADED if (errors or warnings) else HEALTHY)
    status = ERROR if blocking else (WARNING if (errors or warnings) else OK)
    return {"status": status, "health": health,
            "warnings": warnings, "errors": errors,
            "blocking": blocking,
            "degraded": degraded, "optional_offline": optional_offline,
            "checks": checks,
            "data_dir": str(DATA_DIR)}


def _exit_code(report: dict[str, Any]) -> int:
    """0 = rien de bloquant · 1 = bloquant · 2 = erreur interne du Doctor."""
    if report.get("internal_error"):
        return 2
    return 1 if report.get("blocking") else 0


def _render_full(report: dict[str, Any]) -> str:
    lines = ["JARVIS DOCTOR", "─" * 44, ""]
    for check in report["checks"]:
        mark = {OK: "OK", WARNING: "WARNING", ERROR: "ERROR"}[check["status"]]
        # Les détails multi-lignes (Secrets, imports cassés…) restent alignés.
        detail = str(check["detail"]).replace("\n", "\n" + " " * 29)
        lines.append(f"{check['label']:<24}{mark:<9}{detail}")
        if check["recommendation"]:
            lines.append(f"{'':<29}→ {check['recommendation']}")
    lines += ["", f"Status: {report.get('health', '?').upper()}",
              f"{report['warnings']} warning(s)",
              f"{len(report['blocking'])} blocking error(s)"]
    if report["blocking"]:
        lines.append(f"Bloquant : {', '.join(report['blocking'])}")
    if report.get("optional_offline"):
        lines.append("Optionnel hors ligne : " + ", ".join(report["optional_offline"]))
    return "\n".join(lines)


# ------------------------------------------------------------- réparations
# Une réparation = une commande non interactive, sans choix humain à faire.
REPAIRS: dict[str, list[list[str]]] = {
    "piper": [[sys.executable, "-m", "pip", "install", "piper-tts"],
              [sys.executable, "-m", "jarvis.tts", "install"]],
    "stt": [[sys.executable, "-m", "pip", "install", "faster-whisper"]],
    "playwright": [[sys.executable, "-m", "pip", "install", "playwright"],
                   [sys.executable, "-m", "playwright", "install", "chromium"]],
    "discord": [[sys.executable, "-m", "pip", "install", "discord.py"]],
    "vault": [[sys.executable, "-m", "pip", "install", "cryptography"]],
    "clap": [[sys.executable, "-m", "pip", "install", "-r", "requirements-audio.txt"]],
}


def repair(core, names: list[str] | None = None) -> dict[str, Any]:
    """Installe ce qui s'installe seul. Retourne le détail de chaque commande."""
    report = diagnose(core)
    targets = [n for n in (names or report["auto_fixable"]) if n in REPAIRS]
    steps: list[dict[str, Any]] = []
    for name in targets:
        for command in REPAIRS[name]:
            try:
                proc = subprocess.run(
                    command, capture_output=True, text=True, timeout=900,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
                steps.append({"check": name, "command": " ".join(command[1:]),
                              "ok": proc.returncode == 0, "output": "\n".join(tail)})
                if proc.returncode != 0:
                    break  # inutile d'enchaîner sur une étape dont la précédente a échoué
            except Exception as exc:
                steps.append({"check": name, "command": " ".join(command[1:]),
                              "ok": False, "output": str(exc)})
                break
    return {"steps": steps, "attempted": targets,
            "after": diagnose(core, only=targets) if targets else report}


# --------------------------------------------------------------------- CLI
def _render(report: dict[str, Any]) -> str:
    """Rendu historique des seules briques (utilisé par --repair)."""
    lines = [report["summary"], ""]
    for check in report["checks"]:
        mark = "OK    " if check["ok"] else "MANQUE"
        lines.append(f"[{mark}] {check['label']} — {check['detail']}")
        if check["fix"]:
            lines.append(f"         → {check['fix']}")
    return "\n".join(lines)


def _print(text: str) -> None:
    """Sortie console tolérante : un terminal cp1252 ne doit pas faire planter."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _internal_error(exc: Exception, as_json: bool) -> None:
    """Erreur interne du Doctor : signalée, code de sortie 2, jamais masquée."""
    if as_json:
        print(json.dumps({"status": "error", "internal_error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2))
    else:
        _print(f"JARVIS DOCTOR — ERREUR INTERNE\n{type(exc).__name__}: {exc}\n"
               f"{'─' * 44}\n(traceback)\n{''.join(traceback.format_exception(exc)).strip()}")


def cli() -> None:
    """`python -m jarvis.doctor [--json] [--strict] [--repair] [contrôles…]

    Codes de sortie (identiques avec ou sans --strict, le mode strict ne fait
    que les documenter) : 0 = rien de bloquant · 1 = bloquant · 2 = erreur
    interne du Doctor. Les warnings seuls ne valent JAMAIS 1.
    """
    args = sys.argv[1:]
    as_json = "--json" in args
    names = [a for a in args if not a.startswith("-")] or None

    if "--repair" in args:
        from .core import JarvisCore

        core = JarvisCore()
        result = repair(core, names)
        _print(json.dumps(result, ensure_ascii=False, indent=2) if as_json
               else _render(result["after"]))
        sys.exit(0 if all(step["ok"] for step in result["steps"]) else 1)

    try:
        if as_json:
            # Aucun texte parasite en mode JSON : la bannière du Core
            # (JARVIS_BUILD_ID sur stdout à l'init) ne doit pas s'y retrouver.
            with contextlib.redirect_stdout(io.StringIO()):
                report = full_diagnose(names)
        else:
            report = full_diagnose(names)
    except Exception as exc:  # le Doctor lui-même ne doit jamais s'effondrer en silence
        _internal_error(exc, as_json)
        sys.exit(2)

    if as_json:
        # Aucun texte parasite en mode JSON : la sortie est intégralement le JSON.
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(_render_full(report))
    sys.exit(_exit_code(report))


if __name__ == "__main__":
    cli()
