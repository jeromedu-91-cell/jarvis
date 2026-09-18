"""JARVIS Startup Manager — prépare macOS puis lance JARVIS en une commande.

Réutilise le Doctor (`jarvis.doctor`) : il mesure l'état réel, le startup ne
réécrit aucune sonde. Politique stricte :

  * aucun statut inventé — tout vient d'une mesure ;
  * on ne lance que ce qui manque (JARVIS, serveur LLM si un launcher existe) ;
  * jamais de destruction : pas de kill global, pas de suppression. Un
    processus tiers est signalé, jamais tué ;
  * aucune erreur Python masquée : JARVIS tourne au premier plan, stdout et
    stderr restent visibles.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import ROOT
from .doctor import (
    BLOCKED,
    DEGRADED,
    ERROR,
    LLM_API_PORT,
    MAIN_API_PORT,
    OK,
    OPTIONAL,
    _http_json,
    _port_state,
    full_diagnose,
)

# macOS n'a pas besoin du runtime Intel Fortran : signal Ctrl+C géré par JARVIS.

DEFAULT_ENTRYPOINT = ROOT / "jarvis.py"
LOG_DIR = ROOT / "logs" / "startup"
SUPERVISOR_PORT = 8770
COMFY_PORT = 8188

# Contrôles affichés en tête, dans un ordre lisible par un humain.
_HEADLINE = (
    ("python", "Python"),
    ("repository", "Repository"),
    ("imports", "Imports"),
    ("database", "Database"),
    ("eventbus", "EventBus"),
    ("configuration", "Configuration"),
    ("secrets", "Secrets"),
    ("llm_server", "LLM Server"),
    ("llm_model", "LLM Model"),
    ("ports", "Ports"),
    ("api_server", "API Server"),
    ("tool_registry", "Tools"),
    ("mission_control", "Mission Control"),
)
# Briques externes : affichées après, un échec n'est pas bloquant en général.
_TAIL = (
    ("comfyui", "ComfyUI"),
    ("supervisor", "Supervisor"),
    ("blender", "Blender"),
    ("stt", "Dictée (STT)"),
    ("piper", "Voix Piper"),
    ("playwright", "Playwright"),
    ("discord", "Bot Discord"),
    ("gpu", "Mesure GPU"),
    ("brainrot_imports", "Brainrot"),
    ("core", "Core startup"),
    ("external_bricks", "Briques externes"),
    ("llm", "Modèle LLM"),
    ("vault", "Coffre"),
    ("clap", "Double clap"),
    ("n8n", "Connecteur n8n"),
    ("vision", "Vue (avatar)"),
)


def _safe_print(text: str) -> None:
    """Un terminal cp1252 (ou absent, sous pythonw) ne doit pas faire échouer."""
    if sys.stdout is None:  # pythonw.exe : aucune console
        return
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _emit(on_event: Callable[[dict[str, Any]], None] | None, **event: Any) -> None:
    """Publie un événement d'état. Une UI cassée ne doit jamais bloquer le boot."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        pass


def interface_url() -> str:
    """Adresse réelle de l'interface, dérivée de la configuration, jamais figée."""
    host = (os.getenv("JARVIS_HOST") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    try:
        port = int(os.getenv("JARVIS_PORT", str(MAIN_API_PORT)))
    except ValueError:
        port = MAIN_API_PORT
    return f"http://{host}:{port}/"


def open_interface(url: str | None = None) -> bool:
    """Ouvre l'interface via le mécanisme existant (WebUI, sinon navigateur)."""
    target = url or interface_url()
    try:
        import webbrowser

        return bool(webbrowser.open(target))
    except Exception:
        return False


def open_doctor() -> bool:
    """Lance JARVIS_DOCTOR.command pour le diagnostic détaillé, sans le réécrire."""
    command = ROOT / "JARVIS_DOCTOR.command"
    if not command.exists():
        return False
    try:
        # `open` = Finder : Terminal s'ouvre et exécute le script.
        return bool(subprocess.Popen(["open", str(command)]).pid)
    except Exception:
        return False


def _log_tail(path: Path, lines: int = 8) -> str:
    """Dernières lignes utiles d'un log, pour ne masquer aucune erreur Python."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(content[-lines:])


# --------------------------------------------------------------- état réel
def probe_jarvis_health(port: int = MAIN_API_PORT,
                        http_json: Callable[[str], Any] = _http_json) -> dict[str, Any] | None:
    """Confirme que le port 8765 est bien JARVIS (et pas un autre logiciel)."""
    try:
        data = http_json(f"http://127.0.0.1:{port}/api/health")
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok") and "version" in data:
        return data
    return None


def _process_label(state: dict[str, Any]) -> str:
    name = state.get("name") or "processus inconnu"
    pid = state.get("pid")
    return f"{name} (PID {pid})" if pid else name


def find_llama_launcher(root: Path = ROOT,
                        override: str | os.PathLike[str] | None = None) -> Path | None:
    """Cherche un launcher llama.cpp DÉJÀ présent dans le projet.

    Ne devine jamais un chemin de GGUF : seul un script qui invoque
    réellement « llama-server » compte. `JARVIS_LLAMA_LAUNCHER` force le choix.
    """
    if override:
        path = Path(override)
        return path if path.exists() else None
    env_override = os.getenv("JARVIS_LLAMA_LAUNCHER", "").strip()
    if env_override:
        path = Path(env_override)
        return path if path.exists() else None

    skip = {".git", ".venv", "node_modules", "data", "__pycache__", "logs", "ui"}
    markers = ("llama-server", "llama_server", "llama.cpp", "llama-cli")
    try:
        candidates = list(root.glob("*.sh")) + list(root.glob("*.command"))
        for child in root.iterdir():
            if child.is_dir() and child.name not in skip:
                for pattern in ("*.sh", "*.command"):
                    candidates.extend(child.glob(pattern))
    except Exception:
        return None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:
            continue
        if any(marker in text for marker in markers):
            return path
    return None


def _online_providers(report: dict[str, Any]) -> list[str]:
    """Noms des fournisseurs LLM réellement connectés, d'après le Doctor."""
    names: list[str] = []
    for check in report.get("checks", []):
        if check.get("name") != "llm_server":
            continue
        providers = (check.get("details") or {}).get("providers") or []
        for provider in providers:
            if provider.get("connected"):
                label = provider.get("name") or provider.get("type") or "provider"
                if label not in names:
                    names.append(label)
    return names


def llm_state(report: dict[str, Any], port_8080: dict[str, Any],
              launcher: Path | None) -> dict[str, Any]:
    """État LLM réel : serveur local, repli provider, launcher, ou hors ligne."""
    providers = _online_providers(report)
    if port_8080.get("state") == "listening":
        return {"state": "running", "providers": providers,
                "message": "LLM SERVER ALREADY RUNNING (127.0.0.1:8080)"}
    if providers:
        return {"state": "fallback", "providers": providers,
                "message": "LLM SERVER OFFLINE (127.0.0.1:8080)\n"
                           f"LLM STATUS : AVAILABLE VIA {', '.join(providers).upper()}"}
    if launcher is not None:
        return {"state": "launchable", "providers": [], "launcher": str(launcher),
                "message": "LLM SERVER OFFLINE (127.0.0.1:8080)\n"
                           f"LAUNCHER TROUVÉ : {launcher}"}
    return {"state": "offline", "providers": [],
            "message": "LLM SERVER OFFLINE (127.0.0.1:8080)\nMANUAL START REQUIRED"}


# --------------------------------------------------------------- affichage
def _mark(check: dict[str, Any]) -> str:
    status = check.get("status")
    if status == OK:
        return "OK"
    if status == ERROR:
        return "BLOCKED" if check.get("blocking") else "DEGRADED"
    if check.get("severity") == OPTIONAL:
        return "OPTIONAL"
    return "DEGRADED"


def _line(label: str, check: dict[str, Any]) -> str:
    """Aligne le statut sans jamais coller une étiquette trop longue."""
    pad = label if len(label) > 22 else f"{label:<22}"
    return f"{pad}{_mark(check)}"


def render_preflight(report: dict[str, Any]) -> str:
    """Bloc « JARVIS STARTUP » : une ligne par contrôle, jamais un faux statut."""
    by_name = {c["name"]: c for c in report.get("checks", [])}
    shown: set[str] = set()
    lines = ["JARVIS STARTUP", "-" * 33, ""]
    for name, label in _HEADLINE:
        check = by_name.get(name)
        if check is None:
            continue
        shown.add(name)
        lines.append(_line(label, check))
    tail = [(label, by_name[name]) for name, label in _TAIL if name in by_name]
    rest = [c for n, c in by_name.items() if n not in shown
            and n not in {n for n, _ in _TAIL}]
    if tail or rest:
        lines.append("")
    for label, check in tail:
        lines.append(_line(label, check))
    for check in rest:
        lines.append(_line(str(check.get("label", check["name"]))[:22], check))
    health = report.get("health")
    verdict = "BLOCKED" if health == BLOCKED else "READY"
    lines += ["", f"Preflight: {verdict}"]
    return "\n".join(lines)


# --------------------------------------------------------------- préflight
def _run_quiet(run_doctor: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Le Core imprime sa bannière sur stdout : elle ne doit pas polluer l'écran."""
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        return run_doctor()


def preflight(run_doctor: Callable[[], dict[str, Any]] = full_diagnose) -> dict[str, Any]:
    report = _run_quiet(run_doctor)
    health = report.get("health")
    blocked = health == BLOCKED
    return {"report": report, "health": health, "blocked": blocked, "ready": not blocked}


# ------------------------------------------------------------------- logs
def write_log(record: dict[str, Any], log_dir: Path = LOG_DIR) -> Path:
    """Un log léger, sans aucun secret (noms et PID uniquement)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"startup_{datetime.now():%Y-%m-%d}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------- démarrage
def _launch_detached(command: list[str], cwd: Path = ROOT) -> Any:
    """Lance un exécutable externe (launcher LLM) détaché, sans jamais le tuer."""
    # start_new_session : nouvelle session POSIX, indépendante du Terminal.
    return subprocess.Popen(command, cwd=str(cwd), start_new_session=True)


def _run_foreground(entrypoint: Path) -> int:
    """Lance JARVIS au premier plan, dans la même console.

    Le manager a déjà mis SIGINT en SIG_IGN (voir `cli`) : le Ctrl+C qui arrête
    JARVIS ne doit pas être interprété comme un échec de démarrage.
    """
    return int(subprocess.call([sys.executable, str(entrypoint)], cwd=str(ROOT)))


def _launch_jarvis_detached(entrypoint: Path) -> Any:
    """Lance JARVIS détaché du Terminal, sortie dans un log.

    Nouvelle session POSIX : JARVIS survit à la fermeture du Terminal tout en
    restant arrêtable proprement (`JARVIS_STOP.command` envoie un SIGINT à SON
    PID confirmé). `JARVIS_LAUNCH_UI=0` empêche JARVIS d'ouvrir lui-même le
    navigateur : le Boot Screen ouvre l'interface une seule fois, quand le
    serveur est healthy.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "jarvis.log"
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    env["JARVIS_LAUNCH_UI"] = "0"
    return subprocess.Popen([sys.executable, str(entrypoint)], cwd=str(ROOT),
                            stdout=handle, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)


def _launcher_command(path: Path) -> list[str]:
    if path.suffix.lower() in (".sh", ".command"):
        return ["bash", str(path)]
    try:
        executable = bool(path.stat().st_mode & 0o111)
    except Exception:
        executable = False
    if executable:  # binaire autonome (ex. llama-server)
        return [str(path)]
    return ["bash", str(path)]


def start(*, run_doctor: Callable[[], dict[str, Any]] = full_diagnose,
          port_state: Callable[[int], dict[str, Any]] = _port_state,
          probe_health: Callable[[int], dict[str, Any] | None] = probe_jarvis_health,
          spawn: Callable[[list[str]], Any] = _launch_detached,
          runner: Callable[[Path], int] | None = None,
          entrypoint: Path = DEFAULT_ENTRYPOINT,
          launcher: Path | None = None,
          no_launch: bool = False,
          log_dir: Path = LOG_DIR,
          emit: Callable[[str], None] = _safe_print,
          on_event: Callable[[dict[str, Any]], None] | None = None,
          detach: bool = False,
          spawn_jarvis: Callable[[Path], Any] | None = None,
          health_wait_s: float = 90.0,
          health_interval_s: float = 0.6,
          open_ui: bool = False) -> int:
    """Prépare l'environnement puis lance JARVIS. Renvoie 0 (ok) ou 1 (échec).

    `on_event` publie les états réels mesurés (diagnostic du Doctor, LLM, ports,
    lancement, résultat) que le Boot Screen se contente d'afficher. `detach`
    lance JARVIS sans console et attend qu'il soit vraiment healthy.
    """
    _emit(on_event, type="phase", phase="preflight")
    report = _run_quiet(run_doctor)
    emit(render_preflight(report))
    emit("")
    _emit(on_event, type="diagnosis", report=report)

    ports: dict[str, Any] = {MAIN_API_PORT: port_state(MAIN_API_PORT),
                             LLM_API_PORT: port_state(LLM_API_PORT),
                             SUPERVISOR_PORT: port_state(SUPERVISOR_PORT),
                             COMFY_PORT: port_state(COMFY_PORT)}
    port_states = {str(k): v.get("state") for k, v in ports.items()}
    _emit(on_event, type="ports", states=port_states)
    jarvis_port = ports[MAIN_API_PORT]

    # JARVIS tourne déjà ? On ne lance jamais une deuxième instance.
    if jarvis_port.get("state") == "listening":
        health = probe_health(MAIN_API_PORT)
        if health is not None:
            emit(f"JARVIS ALREADY RUNNING — {_process_label(jarvis_port)} "
                 f"· v{health.get('version', '?')} · http://127.0.0.1:{MAIN_API_PORT}/")
            _emit(on_event, type="result", outcome="already_running",
                  version=health.get("version"), port=MAIN_API_PORT,
                  llm=None, ports=port_states)
            write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "already_running", "health": report.get("health"),
                       "ports": port_states, "result": "ok"}, log_dir)
            if detach and open_ui:
                open_interface()
            return 0
        # Port occupé par un AUTRE programme : on signale, on ne tue jamais.
        emit(f"PORT {MAIN_API_PORT} OCCUPÉ PAR {_process_label(jarvis_port)} — "
             f"ce n'est pas JARVIS.\nJARVIS START FAILED : conflit de port.")
        emit("Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé.")
        _emit(on_event, type="result", outcome="blocked", blocking=[],
              message=f"Port {MAIN_API_PORT} occupé par {_process_label(jarvis_port)} "
                      "— ce n'est pas JARVIS.",
              owner=_process_label(jarvis_port), ports=port_states)
        write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "port_conflict", "port": MAIN_API_PORT,
                   "owner": _process_label(jarvis_port), "result": "failed"}, log_dir)
        return 1

    # Serveur LLM : réutiliser ce qui existe, sinon ne pas en démarrer un second.
    found_launcher = launcher if launcher is not None else find_llama_launcher()
    llm = llm_state(report, ports[LLM_API_PORT], found_launcher)
    emit(llm["message"])
    emit("")
    _emit(on_event, type="llm", state=llm["state"], message=llm["message"],
          providers=llm.get("providers", []))

    if llm["state"] == "launchable" and not no_launch and found_launcher is not None:
        try:
            spawn(_launcher_command(found_launcher))
            emit("Serveur LLM lancé (détaché). JARVIS patientera le temps du chargement.")
            _emit(on_event, type="phase", phase="llm_start")
        except Exception as exc:  # le lancement LLM ne doit jamais masquer l'erreur
            emit(f"Lancement du serveur LLM impossible : {type(exc).__name__}: {exc}")

    if report.get("health") == BLOCKED:
        emit("JARVIS START FAILED : dépendance bloquante hors service.")
        emit("Bloquant : " + ", ".join(report.get("blocking", [])))
        emit("Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé.")
        _emit(on_event, type="result", outcome="blocked",
              blocking=report.get("blocking", []), llm=llm["state"],
              message="Dépendance bloquante hors service : "
                      + ", ".join(report.get("blocking", [])) + ".",
              ports=port_states)
        write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "blocked", "health": BLOCKED,
                   "blocking": report.get("blocking", []),
                   "llm": llm["state"], "result": "failed"}, log_dir)
        return 1

    if not entrypoint.exists():
        emit(f"JARVIS START FAILED : point d'entrée introuvable ({entrypoint}).")
        emit("Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé.")
        _emit(on_event, type="result", outcome="failed", llm=llm["state"],
              message=f"Point d'entrée introuvable : {entrypoint}.")
        write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "missing_entrypoint", "entrypoint": str(entrypoint),
                   "result": "failed"}, log_dir)
        return 1

    if no_launch:
        emit(f"[--no-launch] environ prêt, JARVIS ne sera pas lancé ({entrypoint}).")
        _emit(on_event, type="result", outcome="ready", no_launch=True,
              degraded=report.get("degraded", []), llm=llm["state"], ports=port_states)
        return 0

    _emit(on_event, type="phase", phase="launch")
    emit(f"Démarrage de JARVIS ({entrypoint.name})…")
    emit("")
    write_log({"ts": datetime.now().isoformat(timespec="seconds"),
               "event": "launch", "entrypoint": str(entrypoint),
               "health": report.get("health"), "llm": llm["state"],
               "degraded": report.get("degraded", []),
               "ports": port_states, "result": "started"}, log_dir)

    if detach:
        return _start_detached(report, llm, ports, log_dir, entrypoint, detach and open_ui,
                               spawn_jarvis or _launch_jarvis_detached, probe_health,
                               health_wait_s, health_interval_s, emit, on_event)

    run = runner or _run_foreground
    started = time.time()
    try:
        code = int(run(entrypoint))
    except Exception as exc:  # jamais masquer une erreur Python
        emit(f"JARVIS START FAILED : {type(exc).__name__}: {exc}")
        emit("Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé.")
        _emit(on_event, type="result", outcome="failed", llm=llm["state"],
              message=f"{type(exc).__name__}: {exc}")
        write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "launch_exception", "error": f"{type(exc).__name__}: {exc}",
                   "llm": llm["state"], "result": "failed"}, log_dir)
        return 1

    log_path = write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                          "event": "jarvis_exit", "code": code,
                          "uptime_s": round(time.time() - started, 1),
                          "health": report.get("health"), "llm": llm["state"],
                          "ports": port_states,
                          "result": "ok" if code == 0 else "failed"}, log_dir)
    if code != 0:
        emit("")
        emit(f"JARVIS START FAILED : JARVIS s'est arrêté avec le code {code}.")
        emit(f"Log : {log_path}")
        emit("Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé.")
        _emit(on_event, type="result", outcome="failed", llm=llm["state"],
              message=f"JARVIS s'est arrêté avec le code {code}.", log=str(log_path))
        return code if code > 0 else 1
    emit("JARVIS arrêté proprement.")
    _emit(on_event, type="result", outcome="stopped", llm=llm["state"])
    return 0


def _start_detached(report: dict[str, Any], llm: dict[str, Any],
                    ports: dict[str, Any], log_dir: Path, entrypoint: Path,
                    open_ui: bool, spawn_jarvis: Callable[[Path], Any],
                    probe_health: Callable[[int], dict[str, Any] | None],
                    health_wait_s: float, health_interval_s: float,
                    emit: Callable[[str], None],
                    on_event: Callable[[dict[str, Any]], None] | None) -> int:
    """Démarre JARVIS sans console puis attend sa santé réelle via /api/health."""
    port_states = {str(k): v.get("state") for k, v in ports.items()}
    started = time.time()
    try:
        proc = spawn_jarvis(entrypoint)
    except Exception as exc:  # jamais masquer une erreur Python
        emit(f"JARVIS START FAILED : {type(exc).__name__}: {exc}")
        _emit(on_event, type="result", outcome="failed", llm=llm["state"],
              message=f"Lancement impossible : {type(exc).__name__}: {exc}")
        write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                   "event": "launch_exception", "error": f"{type(exc).__name__}: {exc}",
                   "llm": llm["state"], "result": "failed"}, log_dir)
        return 1

    _emit(on_event, type="process", pid=getattr(proc, "pid", None),
          entrypoint=str(entrypoint))
    log_file = log_dir / "jarvis.log"
    deadline = time.time() + max(1.0, health_wait_s)
    while time.time() < deadline:
        health = probe_health(MAIN_API_PORT)
        if health is not None:
            outcome = "degraded" if report.get("health") == DEGRADED else "ready"
            _emit(on_event, type="result", outcome=outcome,
                  version=health.get("version"), degraded=report.get("degraded", []),
                  llm=llm["state"], ports=port_states,
                  uptime_s=round(time.time() - started, 1))
            write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "ready", "outcome": outcome,
                       "uptime_s": round(time.time() - started, 1),
                       "health": report.get("health"), "llm": llm["state"],
                       "degraded": report.get("degraded", []),
                       "ports": port_states, "result": "ok"}, log_dir)
            if open_ui:
                open_interface()
            return 0
        if proc.poll() is not None:
            tail = _log_tail(log_file)
            message = f"JARVIS s'est arrêté pendant le démarrage (code {proc.returncode})."
            emit(message)
            _emit(on_event, type="result", outcome="failed", llm=llm["state"],
                  message=message, detail=tail, log=str(log_file))
            write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "startup_failed", "code": proc.returncode,
                       "llm": llm["state"], "tail": tail, "result": "failed"}, log_dir)
            return 1
        time.sleep(max(0.1, health_interval_s))

    tail = _log_tail(log_file)
    message = f"JARVIS n'a pas répondu sur {interface_url()} à temps."
    emit(message)
    _emit(on_event, type="result", outcome="failed", llm=llm["state"],
          message=message, detail=tail, log=str(log_file))
    write_log({"ts": datetime.now().isoformat(timespec="seconds"),
               "event": "startup_timeout", "llm": llm["state"],
               "tail": tail, "result": "failed"}, log_dir)
    return 1


# --------------------------------------------------------------- arrêt propre
def _send_ctrl_c(pid: int) -> bool:
    """Envoie un vrai SIGINT à JARVIS (jamais un kill forcé).

    Le PID doit avoir été confirmé comme JARVIS au préalable. SIGINT est le
    signal que JARVIS gère déjà (core.shutdown) — pas SIGKILL.
    """
    try:
        os.kill(pid, signal.SIGINT)
        return True
    except Exception:
        return False


def stop(*, port_state: Callable[[int], dict[str, Any]] = _port_state,
         probe_health: Callable[[int], dict[str, Any] | None] = probe_jarvis_health,
         sender: Callable[[int], bool] = _send_ctrl_c,
         wait_s: float = 8.0,
         emit: Callable[[str], None] = _safe_print) -> int:
    """Arrête JARVIS proprement en visant uniquement SON processus confirmé."""
    state = port_state(MAIN_API_PORT)
    if state.get("state") != "listening":
        emit("JARVIS N'EST PAS EN COURS D'EXÉCUTION.")
        return 0
    if probe_health(MAIN_API_PORT) is None:
        emit(f"PORT {MAIN_API_PORT} OCCUPÉ PAR {_process_label(state)} — "
             "ce n'est pas JARVIS.\nAucun arrêt : rien ne sera tué.")
        return 1
    pid = state.get("pid")
    if not pid:
        emit("PID de JARVIS introuvable : arrêt annulé (aucun kill forcé).")
        return 1
    emit(f"Arrêt propre de JARVIS — {_process_label(state)}…")
    # Ce processus reçoit le Ctrl+C qu'il génère (il rejoint la console cible) :
    # il l'ignore, sinon il mourrait avant de confirmer l'arrêt.
    previous: Any = None
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        previous = None
    try:
        if not sender(int(pid)):
            emit("Envoi de Ctrl+C impossible : ferme la fenêtre de JARVIS ou fais Ctrl+C "
                 "(aucun kill forcé).")
            return 1
        deadline = time.time() + max(0.0, wait_s)
        while time.time() < deadline:
            if port_state(MAIN_API_PORT).get("state") != "listening":
                emit("JARVIS arrêté proprement.")
                return 0
            time.sleep(0.5)
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except Exception:
                pass
    emit("JARVIS n'a pas répondu à l'arrêt à temps : ferme sa fenêtre ou fais Ctrl+C "
         "(aucun kill forcé).")
    return 1


# --------------------------------------------------------------------- CLI
def cli(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in args
    check_only = "--check" in args
    no_launch = "--no-launch" in args
    if "--stop" in args:
        return stop()

    if as_json:
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report = full_diagnose()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("health") == BLOCKED else 0

    if check_only:
        result = preflight()
        _safe_print(render_preflight(result["report"]))
        return 1 if result["blocked"] else 0

    # Le Ctrl+C qui arrête JARVIS arrive aussi à ce processus (même console) :
    # on l'ignore pour laisser JARVIS gérer son arrêt et écrire le log.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass
    return start(no_launch=no_launch)


def main(argv: list[str] | None = None) -> int:
    try:
        return cli(argv)
    except KeyboardInterrupt:
        _safe_print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
