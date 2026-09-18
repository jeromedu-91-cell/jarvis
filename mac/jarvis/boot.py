"""JARVIS Boot Screen — affiche l'état RÉEL du démarrage, puis ouvre l'interface.

Aucune progression inventée : chaque ligne de composant vient du rapport du
Doctor (`jarvis.doctor`) et des événements mesurés par `jarvis.startup`
(LLM, ports, lancement, santé `/api/health`). Le Boot Screen se contente
d'afficher ce qui est vrai, puis rend la main.

Séparation nette :
  * `BootModel`      — état pur, testable sans interface (statuts, % réel, journal) ;
  * `BootController` — décisions (ouvrir l'interface, garder la fenêtre, diagnostic) ;
  * `BootScreen`     — vue Tk Canvas animée (loader, HUD, progression, activity feed).
"""
from __future__ import annotations

import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .doctor import ERROR as CHECK_ERROR, OK as CHECK_OK, OPTIONAL as CHECK_OPTIONAL, _http_json
from .startup import (
    LOG_DIR,
    MAIN_API_PORT,
    open_doctor,
    open_interface,
    start,
    write_log,
)

# --------------------------------------------------------------- statuts
WAITING = "waiting"
STARTING = "starting"
READY = "ready"
DEGRADED_ST = "degraded"
FAILED = "failed"
SKIPPED = "skipped"

STATUS_LABEL = {
    WAITING: "…",
    STARTING: "STARTING",
    READY: "READY",
    DEGRADED_ST: "DEGRADED",
    FAILED: "FAILED",
    SKIPPED: "SKIPPED",
}

# Composants affichés : (clé, libellé, nom de check Doctor ou None, critique).
COMPONENTS: tuple[tuple[str, str, str | None, bool], ...] = (
    ("python", "PYTHON", "python", True),
    ("repository", "REPOSITORY", "repository", True),
    ("database", "DATABASE", "database", True),
    ("eventbus", "EVENT BUS", "eventbus", True),
    ("llm", "LLM", "llm", True),
    ("tools", "TOOLS", "tool_registry", True),
    ("core", "CORE", "core", True),
    ("api_server", "API SERVER", "api_server", True),
    ("mission_control", "MISSION CONTROL", "mission_control", False),
    ("brainrot", "BRAINROT", "brainrot_imports", False),
    ("comfyui", "COMFYUI", "comfyui", False),
    ("supervisor", "SUPERVISOR", "supervisor", False),
    ("agents", "AGENTS", None, False),
)

# Libellés de phase réels (déduits de l'état mesuré, jamais simulés).
PHASE_READY = "SYSTÈME PRÊT"
PHASE_ABORT = "DÉMARRAGE INTERROMPU"
PHASE_LLM = "CONNEXION AU LLM"
PHASE_TOOLS = "CHARGEMENT DES OUTILS"
PHASE_AGENTS = "INITIALISATION DES AGENTS"
PHASE_MISSION = "DÉMARRAGE MISSION CONTROL"
PHASE_API = "VÉRIFICATION API"
PHASE_ENV = "VALIDATION DE L'ENVIRONNEMENT"
PHASE_INIT = "INITIALISATION DU SYSTÈME"

_RESULT_ACTIVITY = {
    "ready": "SYSTÈME PRÊT — OUVERTURE DE L'INTERFACE",
    "degraded": "SYSTÈME PRÊT EN MODE DÉGRADÉ",
    "already_running": "JARVIS DÉJÀ EN LIGNE",
    "blocked": "DÉMARRAGE BLOQUÉ — DÉPENDANCE CRITIQUE",
    "failed": "ÉCHEC DU DÉMARRAGE",
    "stopped": "JARVIS ARRÊTÉ",
}


@dataclass
class Component:
    key: str
    label: str
    check: str | None
    critical: bool
    status: str = WAITING
    detail: str = ""


def _status_from_check(check: dict[str, Any]) -> str:
    status = (check.get("status") or "").lower()
    severity = (check.get("severity") or "").lower()
    if status == CHECK_OK:
        return READY
    if status == "warning":
        return DEGRADED_ST
    if status == CHECK_OPTIONAL or severity == CHECK_OPTIONAL:
        return SKIPPED
    if status == CHECK_ERROR:
        return FAILED if severity == "blocking" else DEGRADED_ST
    return WAITING


def _detail_for(check: dict[str, Any]) -> str:
    details = check.get("details") or {}
    if isinstance(details, dict):
        total = details.get("total")
        if total is not None:
            return f"{total}"
        providers = details.get("providers")
        if providers:
            return f"{len(providers)} fournisseur(s)"
    return ""


class BootModel:
    """État pur du Boot Screen — aucune dépendance à Tk ni au réseau."""

    def __init__(self, components: tuple[tuple[str, str, str | None, bool], ...] = COMPONENTS):
        self.components: dict[str, Component] = {
            key: Component(key, label, check, critical)
            for key, label, check, critical in components
        }
        self.phase = "waiting"
        self.outcome: str | None = None
        self.health: str | None = None
        self.blocking: list[str] = []
        self.degraded: list[str] = []
        self.message = ""
        self.detail = ""
        self.llm: dict[str, Any] | None = None
        self.ports: dict[str, Any] = {}
        self.version: str | None = None
        self.pid: int | None = None
        self.uptime_s: float | None = None
        self.no_launch = False
        self.finished = False
        self.activity: list[str] = []

    # ------------------------------------------------------------ ingestion
    def apply(self, event: dict[str, Any]) -> "BootModel":
        kind = event.get("type")
        handler = {
            "phase": self._apply_phase,
            "diagnosis": self._apply_diagnosis,
            "llm": self._apply_llm,
            "ports": self._apply_ports,
            "process": self._apply_process,
            "status": self._apply_status,
            "result": self._apply_result,
        }.get(kind)
        if handler is not None:
            handler(event)
        return self

    def _note(self, text: str) -> None:
        """Journal d'activité — uniquement des faits mesurés, jamais inventés."""
        if not text:
            return
        if self.activity and self.activity[-1] == text:
            return
        if text in self.activity[-4:]:
            return
        self.activity.append(text)
        if len(self.activity) > 120:
            del self.activity[:len(self.activity) - 120]

    def _apply_phase(self, event: dict[str, Any]) -> None:
        self.phase = event.get("phase", self.phase)
        self._note(f"PHASE · {self.progress_phase()}")

    def _apply_diagnosis(self, event: dict[str, Any]) -> None:
        report = event.get("report") or {}
        by_name = {c.get("name"): c for c in report.get("checks", [])}
        for comp in self.components.values():
            if comp.check is None:
                continue
            check = by_name.get(comp.check)
            if check is None:
                continue
            comp.status = _status_from_check(check)
            comp.detail = _detail_for(check)
        self.health = report.get("health", self.health)
        self.blocking = list(report.get("blocking") or [])
        self.degraded = list(report.get("degraded") or [])
        self._note(f"DOCTOR · ENVIRONNEMENT {str(self.health or '').upper()}")
        for comp in self.components.values():
            if comp.check is None or comp.status == WAITING:
                continue
            label = STATUS_LABEL.get(comp.status, comp.status.upper())
            detail = f" ({comp.detail})" if comp.detail else ""
            self._note(f"{comp.label} {label}{detail}")

    def _apply_llm(self, event: dict[str, Any]) -> None:
        state = event.get("state")
        info = {"state": state, "message": event.get("message", ""),
                "providers": list(event.get("providers") or [])}
        self.llm = info
        comp = self.components.get("llm")
        if comp is not None and comp.status == WAITING:
            if state == "offline" and self.health == "blocked":
                comp.status = FAILED
            elif state in ("running", "fallback"):
                comp.status = READY
            elif state == "launchable":
                comp.status = STARTING
            elif state == "offline":
                comp.status = DEGRADED_ST
        if comp is not None and state == "fallback":
            comp.detail = ", ".join(info["providers"])
        providers = info["providers"]
        if state == "running":
            self._note("LLM SERVER ALREADY RUNNING (127.0.0.1:8080)")
        elif state == "fallback":
            self._note(f"LLM STATUS : AVAILABLE VIA {', '.join(providers).upper()}")
        elif state == "launchable":
            self._note("LLM LAUNCHER FOUND — SERVER OFFLINE")
        elif state == "offline":
            self._note("LLM OFFLINE — MANUAL START REQUIRED")

    def _apply_ports(self, event: dict[str, Any]) -> None:
        self.ports = event.get("states") or {}
        for port, state in self.ports.items():
            if state == "listening":
                self._note(f"PORT {port} LISTENING")

    def _apply_process(self, event: dict[str, Any]) -> None:
        self.pid = event.get("pid")
        if self.pid:
            self._note(f"JARVIS PROCESS STARTED (PID {self.pid})")

    def _apply_status(self, event: dict[str, Any]) -> None:
        """Enrichit avec l'état vivant du serveur une fois JARVIS healthy."""
        status = event.get("status") or {}
        core = (status.get("core") or {}) if isinstance(status, dict) else {}
        agents = core.get("agents") or {}
        total = agents.get("total")
        if total is not None:
            comp = self.components["agents"]
            comp.status = READY
            comp.detail = f"{total}"
            self._note(f"AGENTS READY · {total}")
        llms = core.get("llms") or {}
        comp = self.components["llm"]
        if comp.status in (WAITING, STARTING) and llms.get("count"):
            comp.status = READY
        elif comp.status in (WAITING, STARTING) and llms.get("status") == "not_configured":
            comp.status = DEGRADED_ST
        if llms.get("count") is not None and comp.status == READY:
            comp.detail = f"{llms.get('count')} fournisseur(s)"
            self._note(f"LLM PROVIDERS · {llms.get('count')} connecté(s)")
        tools = core.get("tools") or {}
        if tools.get("total") is not None:
            self._note(f"TOOLS REGISTERED · {tools.get('total')}")

    def _apply_result(self, event: dict[str, Any]) -> None:
        self.outcome = event.get("outcome", self.outcome)
        self.version = event.get("version", self.version)
        self.message = event.get("message", self.message)
        self.detail = event.get("detail", self.detail)
        if event.get("degraded") is not None:
            self.degraded = list(event.get("degraded") or [])
        if event.get("blocking"):
            self.blocking = list(event.get("blocking") or [])
        if event.get("llm"):
            self.llm = self.llm or {"state": event.get("llm"), "message": "", "providers": []}
        self.uptime_s = event.get("uptime_s", self.uptime_s)
        self.no_launch = event.get("no_launch", self.no_launch)
        if self.outcome in ("ready", "degraded", "already_running", "blocked", "failed"):
            self.finished = self.outcome in ("ready", "degraded", "already_running")
            for comp in self.components.values():
                if comp.status == STARTING:
                    comp.status = READY if self.outcome in ("ready", "degraded", "already_running") else FAILED
            note = _RESULT_ACTIVITY.get(self.outcome or "")
            if note:
                self._note(note)
            if self.outcome == "failed" and self.detail:
                for line in str(self.detail).splitlines()[-3:]:
                    self._note(f"LOG · {line.strip()}")

    # ------------------------------------------------------------- lecture
    def ready_count(self) -> int:
        return sum(1 for c in self.components.values() if c.status in (READY,))

    def total_count(self) -> int:
        return len(self.components)

    def summary(self) -> str:
        return f"{self.ready_count()} / {self.total_count()} COMPOSANTS PRÊTS"

    def ratio(self) -> float:
        total = self.total_count()
        return (self.ready_count() / total) if total else 0.0

    def progress_percent(self) -> int:
        """Pourcentage RÉEL, calculé depuis les composants prêts — jamais simulé."""
        return int(round(self.ratio() * 100))

    def progress_phase(self) -> str:
        """Phase lisible, déduite de l'état réel mesuré."""
        if self.outcome in ("ready", "degraded", "already_running"):
            return PHASE_READY
        if self.outcome in ("blocked", "failed"):
            return PHASE_ABORT
        if self.phase == "llm_start":
            return PHASE_LLM
        if self.phase == "launch":
            if self.components["agents"].status in (WAITING, STARTING):
                return PHASE_AGENTS
            if self.components["mission_control"].status in (WAITING, STARTING):
                return PHASE_MISSION
            return PHASE_API
        if self.health is not None:
            return PHASE_TOOLS
        if self.phase == "preflight":
            return PHASE_ENV
        return PHASE_INIT

    def degraded_services(self) -> list[Component]:
        return [c for c in self.components.values()
                if c.status in (DEGRADED_ST, SKIPPED, FAILED) and not c.critical]

    def degraded_line(self) -> str:
        services = self.degraded or [c.key for c in self.degraded_services()]
        if not services:
            return ""
        return "MODE DÉGRADÉ · " + ", ".join(str(s).upper() for s in services[:6])

    def failed_component(self) -> str:
        if self.blocking:
            return ", ".join(str(b).upper() for b in self.blocking)
        failed = [c.label for c in self.components.values() if c.status == FAILED]
        return ", ".join(failed) if failed else "—"

    def banner(self) -> tuple[str, str]:
        """(texte, tonalité) — tonalité : ok | warn | err | info."""
        if self.outcome == "ready":
            return "JARVIS READY", "ok"
        if self.outcome == "degraded":
            return "JARVIS READY", "warn"
        if self.outcome == "already_running":
            return "JARVIS ALREADY ONLINE", "ok"
        if self.outcome in ("blocked", "failed"):
            return "JARVIS START FAILED", "err"
        if self.outcome == "stopped":
            return "JARVIS STOPPED", "info"
        return "SYSTÈME EN INITIALISATION", "info"

    def banner_sub(self) -> str:
        if self.outcome == "degraded":
            return self.degraded_line()
        if self.outcome in ("blocked", "failed"):
            return self.message or "Utilise JARVIS_DOCTOR.command pour le diagnostic détaillé."
        if self.outcome in ("ready", "already_running"):
            version = f" · v{self.version}" if self.version else ""
            return f"{self.summary()}{version}"
        return self.progress_phase()


# ------------------------------------------------------------- contrôleur
class BootController:
    """Décide quoi faire à chaque état — testable sans fenêtre Tk."""

    def __init__(self, view: Any, *, model: BootModel | None = None,
                 starter: Callable[[Callable[[dict], None]], int] | None = None,
                 open_interface_fn: Callable[[], bool] = open_interface,
                 open_doctor_fn: Callable[[], bool] = open_doctor,
                 status_probe: Callable[[], dict[str, Any] | None] | None = None,
                 close_delay_s: float = 2.4):
        self.view = view
        self.model = model or BootModel()
        self._starter = starter
        self._open_interface = open_interface_fn
        self._open_doctor = open_doctor_fn
        self._status_probe = status_probe or probe_status
        self.close_delay_s = close_delay_s
        self.interface_opened = False
        self.diagnostic_opened = False
        self.finished = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- cycle
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._worker, daemon=True, name="jarvis-boot")
        thread.start()
        return thread

    def _worker(self) -> None:
        try:
            if self._starter is not None:
                code = int(self._starter(self.publish))
            else:
                code = int(start(detach=True, open_ui=False, on_event=self.publish,
                                 emit=lambda *_: None))
            if code == 0 and self.model.outcome in ("ready", "degraded"):
                try:
                    status = self._status_probe()
                    if status:
                        self.handle_event({"type": "status", "status": status})
                except Exception:
                    pass
        except Exception as exc:  # une UI ne doit jamais masquer une erreur Python
            self.handle_event({"type": "result", "outcome": "failed",
                               "message": f"Erreur interne du Boot Screen : "
                                          f"{type(exc).__name__}: {exc}"})

    def publish(self, event: dict[str, Any]) -> None:
        """Appelé depuis le thread worker : la vue assure la main-forte à Tk."""
        self.view.post(event)

    # ----------------------------------------------------------- décisions
    def handle_event(self, event: dict[str, Any]) -> None:
        self.model.apply(event)
        self.view.render(self.model)
        if event.get("type") != "result":
            return
        outcome = self.model.outcome
        if outcome in ("ready", "degraded", "already_running"):
            self._finish_online(outcome)
        elif outcome in ("blocked", "failed"):
            self._boot_log(event)
            self.view.show_failure(self.model)

    def _finish_online(self, outcome: str) -> None:
        with self._lock:
            if self.finished:
                return
            self.finished = True
        self.interface_opened = bool(self._open_interface())
        self._boot_log({"type": "result", "outcome": outcome,
                        "version": self.model.version,
                        "degraded": self.model.degraded,
                        "interface_opened": self.interface_opened})
        self.view.show_outcome(outcome, self.model)
        self.view.schedule_close(self.close_delay_s)

    def request_doctor(self) -> bool:
        """Bouton « OUVRIR LE DIAGNOSTIC » : lance JARVIS_DOCTOR.command."""
        self.diagnostic_opened = bool(self._open_doctor())
        self._boot_log({"type": "diagnostic_opened", "ok": self.diagnostic_opened})
        return self.diagnostic_opened

    def request_close(self) -> None:
        self.view.close()

    def _boot_log(self, record: dict[str, Any]) -> None:
        try:
            write_log({"ts": datetime.now().isoformat(timespec="seconds"),
                       "event": "boot_screen", **record}, LOG_DIR)
        except Exception:
            pass


def probe_status(port: int = MAIN_API_PORT) -> dict[str, Any] | None:
    """État vivant du serveur (agents, fournisseurs) une fois healthy."""
    try:
        data = _http_json(f"http://127.0.0.1:{port}/api/status")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------- palette
BG = "#04070d"
PANEL = "#070d16"
PANEL2 = "#091424"
GRID = "#0a1826"
GRID2 = "#0e2131"
BORDER = "#12283a"
BORDER2 = "#0b1a28"
TEXT = "#dbe7f3"
DIM = "#33465b"
CYAN = "#22d3ee"
CYAN_DIM = "#0e5d6b"
OK = "#34d399"
WARN = "#fbbf24"
ERR = "#f87171"
SKIP = "#5b6b7f"
WHITE = "#f8fdff"

STATUS_COLOR = {
    WAITING: DIM,
    STARTING: CYAN,
    READY: OK,
    DEGRADED_ST: WARN,
    FAILED: ERR,
    SKIPPED: SKIP,
}
TONE_COLOR = {"ok": OK, "warn": WARN, "err": ERR, "info": CYAN}

# Base design resolution; everything is scaled by the display DPI.
BASE_W, BASE_H = 1120, 680
TICK_MS = 28

# Layout (base pixels).
HEADER_H = 62
LEFT_X0, LEFT_X1 = 20, 356
DIV_X = 372
MID_X0, MID_X1 = 388, 774
RIGHT_X0, RIGHT_X1 = 790, 1100
FOOTER_Y = 556
LOADER_CX, LOADER_CY, LOADER_R = 188, 236, 116


def animations_enabled() -> bool:
    """Animations actives par défaut ; désactivables pour accessibilité/légèreté."""
    for name in ("JARVIS_REDUCED_MOTION", "JARVIS_NO_ANIM", "JARVIS_BOOT_ANIMATIONS"):
        value = (os.getenv(name) or "").strip().lower()
        if not value:
            continue
        if name == "JARVIS_BOOT_ANIMATIONS":
            if value in {"0", "false", "no", "off"}:
                return False
        elif value in {"1", "true", "yes", "on"}:
            return False
    return True


def _lerp(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


def _pick_font(root: Any, candidates: tuple[str, ...], size: int,
               weight: str = "normal") -> tuple[str, int, str]:
    try:
        families = set(root.tk.call("font", "families"))
    except Exception:
        families = set()
    for name in candidates:
        if name in families:
            return (name, size, weight)
    return (candidates[-1], size, weight)


def _ui_scale(root: Any) -> float:
    """Facteur DPI (125 %/150 %) pour ne jamais couper le texte."""
    try:
        scale = root.winfo_fpixels("1i") / 96.0
    except Exception:
        scale = 1.0
    scale = max(1.0, min(2.0, scale))
    try:
        max_w = root.winfo_screenwidth() * 0.94 / BASE_W
        max_h = root.winfo_screenheight() * 0.90 / BASE_H
        scale = max(1.0, min(scale, max_w, max_h))
    except Exception:
        pass
    return scale


class BootScreen:
    """Fenêtre HUD sombre dessinée au Canvas. Thread-safe via file d'events."""

    def __init__(self, root: Any, model: BootModel):
        self.root = root
        self.model = model
        self.controller: BootController | None = None
        self.canvas: Any = None
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._s = _ui_scale(root)
        self._closing = False
        self._failure_drawn = False
        self._pulse = 0.0
        self._angle = [0.0, 0.0, 0.0]
        self._disp_progress = 0.0
        self._row_born: dict[str, float] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self._last_status: dict[str, str] = {}
        self._status_flash: dict[str, float] = {}
        self._activity_items: list[tuple[Any, float]] = []
        self._activity_count = -1
        self._transition_start: float | None = None
        self._flash_start: float | None = None
        self._final_text: Any = None
        self._final_born: float | None = None
        self._tone = "info"
        self._font_title = None
        self._font_sub = None
        self._font_panel = None
        self._font_label = None
        self._font_status = None
        self._font_small = None
        self._font_phase = None
        self._font_count = None
        self._font_banner = None
        self._tick_after: str | None = None
        self._drain_after: str | None = None
        self._close_after: str | None = None
        self._build()

    # --------------------------------------------------------------- helpers
    def P(self, x: float, y: float) -> tuple[float, float]:
        return (x * self._s, y * self._s)

    def M(self, v: float) -> float:
        return v * self._s

    def F(self, font: tuple[str, int, str]) -> tuple[str, int, str]:
        return (font[0], max(1, int(round(font[1] * self._s))), font[2])

    # --------------------------------------------------------------- setup
    def _build(self) -> None:
        import tkinter as tk

        root = self.root
        root.title("JARVIS")
        root.configure(bg=BG)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        win_w, win_h = int(BASE_W * self._s), int(BASE_H * self._s)
        x = max(0, (root.winfo_screenwidth() - win_w) // 2)
        y = max(0, (root.winfo_screenheight() - win_h) // 3)
        root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        root.bind("<Escape>", lambda _e: self.close())
        self._win_size = (win_w, win_h)

        self._font_title = _pick_font(root, ("Segoe UI Semibold", "Segoe UI", "Consolas", "TkDefaultFont"), 27, "bold")
        self._font_sub = _pick_font(root, ("Segoe UI", "Consolas", "TkDefaultFont"), 9)
        self._font_panel = _pick_font(root, ("Segoe UI Semibold", "Consolas", "TkDefaultFont"), 10, "bold")
        self._font_label = _pick_font(root, ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New", "TkFixedFont"), 12)
        self._font_status = _pick_font(root, ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New", "TkFixedFont"), 12, "bold")
        self._font_small = _pick_font(root, ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New", "TkFixedFont"), 9)
        self._font_phase = _pick_font(root, ("Segoe UI Semibold", "Segoe UI", "Consolas", "TkDefaultFont"), 13, "bold")
        self._font_count = _pick_font(root, ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New", "TkFixedFont"), 13, "bold")
        self._font_banner = _pick_font(root, ("Segoe UI Semibold", "Segoe UI", "Consolas", "TkDefaultFont"), 30, "bold")

        self.canvas = tk.Canvas(root, width=win_w, height=win_h, bg=BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._build_background()
        self._build_header()
        self._build_loader()
        self._build_ai_core()
        self._build_components()
        self._build_activity()
        self._build_footer()
        self._build_final_overlay()

        # Déplacement de la fenêtre sans barre de titre.
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)

    # ------------------------------------------------------------ background
    def _build_background(self) -> None:
        c = self.canvas
        c.create_rectangle(0, 0, self.M(BASE_W), self.M(BASE_H), fill=BG, outline="")
        c.create_rectangle(1, 1, self.M(BASE_W - 1), self.M(BASE_H - 1), outline=BORDER)

        step = 42
        for gx in range(0, BASE_W + 1, step):
            c.create_line(*self.P(gx, HEADER_H), *self.P(gx, FOOTER_Y), fill=GRID)
        for gy in range(HEADER_H, FOOTER_Y + 1, step):
            c.create_line(*self.P(0, gy), *self.P(BASE_W, gy), fill=GRID)
        # Grille plus marquée dans les zones de panneaux.
        for gy in range(HEADER_H, FOOTER_Y + 1, step * 3):
            c.create_line(*self.P(0, gy), *self.P(BASE_W, gy), fill="#0c1c2b")

        # Crochets d'angle HUD.
        m = 10
        for cx, cy, dx, dy in ((m, m, 1, 1), (BASE_W - m, m, -1, 1),
                               (m, BASE_H - m, 1, -1), (BASE_W - m, BASE_H - m, -1, -1)):
            c.create_line(*self.P(cx, cy), *self.P(cx + dx * 34, cy), *self.P(cx, cy + dy * 20),
                          fill=CYAN_DIM, width=max(1, int(self._s)))
        # Graduations latérales.
        for gy in range(HEADER_H + 20, FOOTER_Y, 34):
            c.create_line(*self.P(6, gy), *self.P(12, gy), fill=BORDER2)
            c.create_line(*self.P(BASE_W - 12, gy), *self.P(BASE_W - 6, gy), fill=BORDER2)

        # Scanline verticale animée (très discrète).
        self._bg_scan = c.create_line(*self.P(0, HEADER_H), *self.P(BASE_W, HEADER_H),
                                      fill="#0b2230", width=self.M(2))

    # ---------------------------------------------------------------- header
    def _build_header(self) -> None:
        c = self.canvas
        c.create_line(*self.P(20, HEADER_H - 2), *self.P(BASE_W - 20, HEADER_H - 2), fill=BORDER)
        c.create_text(*self.P(24, 31), anchor="w", text="J A R V I S", fill=TEXT,
                      font=self.F(self._font_title))
        c.create_text(*self.P(BASE_W // 2, 26), anchor="center", text="SYSTEM INITIALIZATION",
                      fill=CYAN_DIM, font=self.F(self._font_sub))
        c.create_text(*self.P(BASE_W // 2, 42), anchor="center", text="NEURAL BOOT SEQUENCE",
                      fill=DIM, font=self.F(self._font_sub))
        # Statut droit + horloge.
        self._header_clock = c.create_text(*self.P(BASE_W - 54, 26), anchor="e", text="",
                                           fill=DIM, font=self.F(self._font_small))
        self._header_state = c.create_text(*self.P(BASE_W - 54, 42), anchor="e", text="ONLINE CHECK",
                                           fill=CYAN_DIM, font=self.F(self._font_small))
        # Croix de fermeture.
        self._close_btn = c.create_text(*self.P(BASE_W - 24, 24), anchor="center", text="\u2715",
                                        fill=DIM, font=(self._font_sub[0], int(12 * self._s)))
        c.tag_bind(self._close_btn, "<Button-1>", lambda _e: self.close())
        c.tag_bind(self._close_btn, "<Enter>", lambda _e: c.itemconfigure(self._close_btn, fill=ERR))
        c.tag_bind(self._close_btn, "<Leave>", lambda _e: c.itemconfigure(self._close_btn, fill=DIM))

    # ---------------------------------------------------------------- loader
    def _build_loader(self) -> None:
        c = self.canvas
        c.create_text(*self.P(LEFT_X0, 82), anchor="w", text="NEURAL CORE",
                      fill=CYAN_DIM, font=self.F(self._font_panel))
        cx, cy = LOADER_CX, LOADER_CY
        # Fond du loader.
        c.create_oval(*self.P(cx - LOADER_R - 20, cy - LOADER_R - 20),
                      *self.P(cx + LOADER_R + 20, cy + LOADER_R + 20),
                      outline=BORDER2, fill=PANEL)
        # Anneau externe (3 arcs).
        self._ring_outer = [
            c.create_arc(*self.P(cx - LOADER_R, cy - LOADER_R),
                         *self.P(cx + LOADER_R, cy + LOADER_R),
                         start=0, extent=76, style="arc", outline=CYAN,
                         width=max(1, int(3 * self._s)))
            for _ in range(3)
        ]
        # Anneau intermédiaire (4 arcs, sens inverse).
        r2 = LOADER_R - 24
        self._ring_mid = [
            c.create_arc(*self.P(cx - r2, cy - r2), *self.P(cx + r2, cy + r2),
                         start=0, extent=54, style="arc", outline=CYAN_DIM,
                         width=max(1, int(2 * self._s)))
            for _ in range(4)
        ]
        # Segments rapides.
        r3 = LOADER_R - 46
        self._ring_seg = [
            c.create_arc(*self.P(cx - r3, cy - r3), *self.P(cx + r3, cy + r3),
                         start=0, extent=14, style="arc", outline=CYAN,
                         width=max(1, int(4 * self._s)))
            for _ in range(8)
        ]
        # Graduations.
        for i in range(48):
            ang = math.radians(i * 7.5)
            r_in = LOADER_R + 6
            r_out = LOADER_R + (16 if i % 4 == 0 else 10)
            c.create_line(*self.P(cx + r_in * math.cos(ang), cy + r_in * math.sin(ang)),
                          *self.P(cx + r_out * math.cos(ang), cy + r_out * math.sin(ang)),
                          fill=BORDER2)
        # Noyau + halos.
        self._core_glow = [
            c.create_oval(*self.P(cx - 34, cy - 34), *self.P(cx + 34, cy + 34),
                          fill=PANEL2, outline="") for _ in range(3)
        ]
        self._core = c.create_oval(*self.P(cx - 20, cy - 20), *self.P(cx + 20, cy + 20),
                                   fill=CYAN, outline="")
        self._core_ring = c.create_oval(*self.P(cx - 27, cy - 27), *self.P(cx + 27, cy + 27),
                                        outline=CYAN, width=max(1, int(1 * self._s)))
        self._scan_ray = c.create_line(*self.P(cx, cy), *self.P(cx + LOADER_R, cy),
                                       fill=CYAN_DIM, width=max(1, int(self._s)))
        self._loader_caption = c.create_text(*self.P(cx, cy + LOADER_R + 40), anchor="center",
                                             text="", fill=DIM, font=self.F(self._font_small))

    # --------------------------------------------------------------- AI core
    def _build_ai_core(self) -> None:
        c = self.canvas
        c.create_text(*self.P(LEFT_X0, 404), anchor="w", text="AI CORE",
                      fill=CYAN_DIM, font=self.F(self._font_panel))
        box = (60, 416, 316, 540)
        c.create_rectangle(*self.P(box[0], box[1]), *self.P(box[2], box[3]),
                           outline=BORDER2, fill=PANEL)
        self._nodes = []
        self._edges = []
        self._signals = []
        positions = [(110, 452), (170, 440), (240, 452), (196, 492),
                     (128, 512), (262, 512), (188, 466), (150, 478), (232, 478)]
        edges = [(0, 2), (0, 3), (2, 3), (3, 4), (3, 5), (4, 5), (6, 0), (6, 2),
                 (6, 4), (6, 5), (7, 6), (8, 6), (7, 8)]
        for a, b in edges:
            x1, y1 = positions[a]
            x2, y2 = positions[b]
            self._edges.append(c.create_line(*self.P(x1, y1), *self.P(x2, y2),
                                             fill=BORDER, width=max(1, int(self._s))))
        for nx, ny in positions:
            self._nodes.append(c.create_oval(*self.P(nx - 4, ny - 4), *self.P(nx + 4, ny + 4),
                                             fill=CYAN_DIM, outline=""))
        for i, (a, b) in enumerate((edges[0], edges[2], edges[4], edges[8], edges[10])):
            x1, y1 = positions[a]
            x2, y2 = positions[b]
            dot = c.create_oval(*self.P(x1 - 2, y1 - 2), *self.P(x1 + 2, y1 + 2),
                                fill=CYAN, outline="")
            self._signals.append((dot, x1, y1, x2, y2, i * 0.2))

    # ------------------------------------------------------------ components
    def _build_components(self) -> None:
        c = self.canvas
        c.create_line(*self.P(DIV_X, HEADER_H + 12), *self.P(DIV_X, FOOTER_Y - 12), fill=BORDER2)
        c.create_text(*self.P(MID_X0, 82), anchor="w", text="COMPONENTS",
                      fill=CYAN_DIM, font=self.F(self._font_panel))
        c.create_text(*self.P(MID_X1, 82), anchor="e", text="STATUS",
                      fill=DIM, font=self.F(self._font_small))
        top, step = 110, 30
        now = time.time()
        for index, (key, comp) in enumerate(self.model.components.items()):
            y = top + index * step
            glyph = c.create_polygon(self._diamond(MID_X0 + 4, y, 5), fill=PANEL2, outline=DIM)
            label = c.create_text(*self.P(MID_X0 + 18, y), anchor="w", text=comp.label,
                                  fill=DIM, font=self.F(self._font_label))
            status = c.create_text(*self.P(MID_X1, y), anchor="e", text="…",
                                   fill=DIM, font=self.F(self._font_status))
            sep = c.create_line(*self.P(MID_X0, y + 14), *self.P(MID_X1, y + 14), fill=PANEL2)
            self._rows[key] = {"glyph": glyph, "label": label, "status": status,
                               "sep": sep, "x": MID_X0 + 18, "y": y}
            self._row_born[key] = now + index * 0.03
            self._last_status[key] = WAITING

    def _diamond(self, x: float, y: float, r: float) -> list[float]:
        pts = [(x, y - r), (x + r, y), (x, y + r), (x - r, y)]
        out: list[float] = []
        for px, py in pts:
            out.extend(self.P(px, py))
        return out

    # --------------------------------------------------------------- activity
    def _build_activity(self) -> None:
        c = self.canvas
        c.create_line(*self.P(RIGHT_X0 - 16, HEADER_H + 12), *self.P(RIGHT_X0 - 16, FOOTER_Y - 12),
                      fill=BORDER2)
        c.create_text(*self.P(RIGHT_X0, 82), anchor="w", text="ACTIVITY",
                      fill=CYAN_DIM, font=self.F(self._font_panel))
        c.create_rectangle(*self.P(RIGHT_X0 - 4, 96), *self.P(RIGHT_X1, FOOTER_Y - 16),
                           outline=BORDER2, fill=PANEL)

    # ----------------------------------------------------------------- footer
    def _build_footer(self) -> None:
        c = self.canvas
        c.create_line(*self.P(20, FOOTER_Y), *self.P(BASE_W - 20, FOOTER_Y), fill=BORDER)
        self._phase_text = c.create_text(*self.P(24, FOOTER_Y + 22), anchor="w",
                                         text=PHASE_INIT, fill=CYAN,
                                         font=self.F(self._font_phase))
        self._count_text = c.create_text(*self.P(BASE_W - 24, FOOTER_Y + 22), anchor="e",
                                         text="0 / 13   ·   0 %", fill=TEXT,
                                         font=self.F(self._font_count))
        self._sub_text = c.create_text(*self.P(24, FOOTER_Y + 42), anchor="w", text="",
                                       fill=DIM, font=self.F(self._font_small))
        tx0, tx1 = 24, BASE_W - 24
        ty = FOOTER_Y + 66
        self._bar_track = c.create_rectangle(*self.P(tx0, ty), *self.P(tx1, ty + 9),
                                             outline=BORDER, fill=PANEL)
        self._bar_fill = c.create_rectangle(*self.P(tx0, ty), *self.P(tx0, ty + 9),
                                            outline="", fill=CYAN)
        self._bar_glow = c.create_rectangle(*self.P(tx0, ty - 3), *self.P(tx0, ty + 12),
                                            outline="", fill=CYAN_DIM, stipple="gray25")
        self._bar_tick = c.create_rectangle(*self.P(tx0, ty), *self.P(tx0, ty + 9),
                                            outline="", fill=WHITE)
        self._bar_x = (tx0, tx1, ty, 9)

        # Zone d'échec (masquée tant que tout va bien).
        self._fail_items = [
            c.create_text(*self.P(24, FOOTER_Y + 22), anchor="w", text="",
                          fill=ERR, font=self.F(self._font_phase), state="hidden"),
            c.create_text(*self.P(24, FOOTER_Y + 46), anchor="w", text="",
                          fill=TEXT, font=self.F(self._font_small), state="hidden"),
            c.create_text(*self.P(24, FOOTER_Y + 68), anchor="w", text="",
                          fill=DIM, font=self.F(self._font_small), state="hidden"),
            c.create_text(*self.P(24, FOOTER_Y + 92), anchor="w", text="",
                          fill=WARN, font=self.F(self._font_small), state="hidden"),
        ]

    def _build_final_overlay(self) -> None:
        c = self.canvas
        self._final_text = c.create_text(*self.P(BASE_W // 2, BASE_H // 2 - 10), anchor="center",
                                         text="", fill=BG, state="hidden",
                                         font=self.F((self._font_banner[0], 34, "bold")))
        self._final_glow = c.create_text(*self.P(BASE_W // 2, BASE_H // 2 - 10), anchor="center",
                                         text="", fill=BG, state="hidden",
                                         font=self.F((self._font_banner[0], 34, "bold")))
        self._flash = c.create_rectangle(*self.P(0, 0), *self.P(BASE_W, BASE_H),
                                         fill=CYAN, outline="", stipple="gray12", state="hidden")

    # ------------------------------------------------------- thread → Tk
    def post(self, event: dict[str, Any]) -> None:
        self._queue.put(event)

    def bind_controller(self, controller: BootController) -> None:
        self.controller = controller

    def _drag_start(self, event: Any) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event: Any) -> None:
        if not getattr(self, "_drag", None):
            return
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _drain(self) -> None:
        try:
            while True:
                event = self._queue.get_nowait()
                if self.controller is not None:
                    self.controller.handle_event(event)
        except queue.Empty:
            pass
        except Exception as exc:  # jamais masquer
            self.show_failure_message(f"Erreur UI : {type(exc).__name__}: {exc}")
        if not self._closing:
            self._drain_after = self.root.after(33, self._drain)

    # ------------------------------------------------------------ rendu
    def render(self, model: BootModel) -> None:
        if self.canvas is None:
            return
        now = time.time()
        self._tone = model.banner()[1]
        self._render_rows(model, now)
        self._render_banner(model, now)
        self._render_activity(model, now)
        self._render_progress(model, now)

    def _render_rows(self, model: BootModel, now: float) -> None:
        c = self.canvas
        for key, comp in model.components.items():
            row = self._rows.get(key)
            if row is None:
                continue
            reveal = 1.0 if not animations_enabled() else min(
                1.0, max(0.0, (now - self._row_born[key]) / 0.35))
            if self._last_status.get(key) != comp.status:
                self._last_status[key] = comp.status
                self._status_flash[key] = now
            flash = max(0.0, 1.0 - (now - self._status_flash.get(key, 0.0)) / 0.30)

            color = STATUS_COLOR.get(comp.status, DIM)
            if comp.status == STARTING and animations_enabled():
                color = _lerp(CYAN_DIM, CYAN, 0.5 + 0.5 * abs(math.sin(self._pulse * 3)))
            elif comp.status == DEGRADED_ST and animations_enabled():
                color = _lerp(color, "#d99a12", 0.35 * (0.5 + 0.5 * math.sin(self._pulse * 2)))
            elif comp.status == FAILED and animations_enabled():
                color = _lerp(color, "#a53a3a", 0.4 * (0.5 + 0.5 * math.sin(self._pulse * 2.4)))
            if flash > 0:
                color = _lerp(color, WHITE, min(0.85, flash))
            color = _lerp(PANEL, color, reveal)

            label = STATUS_LABEL.get(comp.status, comp.status.upper())
            if comp.status == STARTING and animations_enabled():
                label = "STARTING" + "." * (1 + int(now * 3) % 3)
            if comp.detail:
                label = f"{label} · {comp.detail}"
            dx = (1.0 - reveal) * 12 if animations_enabled() else 0.0
            c.itemconfigure(row["label"], fill=_lerp(PANEL, TEXT, reveal))
            c.coords(row["label"], *self.P(row["x"] + dx, row["y"]))
            c.itemconfigure(row["status"], text=label, fill=color)
            c.coords(row["status"], *self.P(MID_X1 + dx, row["y"]))
            c.itemconfigure(row["sep"], fill=_lerp(PANEL, PANEL2, reveal))
            c.itemconfigure(row["glyph"], fill=_lerp(PANEL2, color, reveal),
                            outline=_lerp(BORDER, color, reveal))

    def _render_banner(self, model: BootModel, now: float) -> None:
        text, tone = model.banner()
        color = TONE_COLOR.get(tone, CYAN)
        if getattr(self, "_phase_text", None):
            self.canvas.itemconfigure(self._phase_text, text=model.progress_phase(), fill=color)
        if getattr(self, "_sub_text", None):
            self.canvas.itemconfigure(self._sub_text, text=model.banner_sub(), fill=DIM)
        if getattr(self, "_header_state", None):
            self.canvas.itemconfigure(self._header_state,
                                      text="ONLINE" if model.finished else "SYNC…",
                                      fill=color)
        if getattr(self, "_header_clock", None):
            self.canvas.itemconfigure(self._header_clock,
                                      text=datetime.now().strftime("%H:%M:%S"))

    def _render_activity(self, model: BootModel, now: float) -> None:
        if self._activity_count == len(model.activity):
            return
        self._activity_count = len(model.activity)
        for item, _born in self._activity_items:
            self.canvas.delete(item)
        self._activity_items = []
        lines = model.activity[-19:]
        top, step = 112, 22
        for index, text in enumerate(lines):
            y = top + index * step
            fresh = index >= len(lines) - 1
            item = self.canvas.create_text(
                *self.P(RIGHT_X0 + 4, y), anchor="w", text=f"> {text}",
                fill=PANEL if fresh and animations_enabled() else TEXT,
                font=self.F(self._font_small))
            self._activity_items.append((item, now if fresh else now - 1.0))

    def _render_progress(self, model: BootModel, now: float) -> None:
        percent = model.progress_percent()
        if getattr(self, "_count_text", None) is not None:
            self.canvas.itemconfigure(
                self._count_text,
                text=f"{model.ready_count()} / {model.total_count()}   ·   {percent} %",
                fill=TEXT)
        target = model.ratio()
        if animations_enabled():
            self._disp_progress += (target - self._disp_progress) * 0.18
        else:
            self._disp_progress = target
        tx0, tx1, ty, h = self._bar_x
        x = tx0 + (tx1 - tx0) * max(0.0, min(1.0, self._disp_progress))
        self.canvas.coords(self._bar_fill, *self.P(tx0, ty), *self.P(x, ty + h))
        self.canvas.coords(self._bar_glow, *self.P(tx0, ty - 3), *self.P(x, ty + h + 3))
        color = TONE_COLOR.get(self._tone, CYAN)
        self.canvas.itemconfigure(self._bar_fill, fill=color)
        self.canvas.itemconfigure(self._bar_glow, fill=_lerp(BG, color, 0.35))

    # ---------------------------------------------------- transition finale
    def _start_transition(self) -> None:
        if self._transition_start is not None or not self.model.finished:
            return
        self._transition_start = time.time()
        self._flash_start = time.time()
        self.canvas.itemconfigure(self._flash, state="normal")

    def _render_transition(self, now: float) -> None:
        if self._transition_start is None:
            return
        t = min(1.0, (now - self._transition_start) / 1.5)
        # Flash cyan bref.
        if self._flash_start is not None:
            ft = (now - self._flash_start) / 0.45
            if ft >= 1.0:
                self.canvas.itemconfigure(self._flash, state="hidden")
                self._flash_start = None
            else:
                self.canvas.itemconfigure(
                    self._flash, stipple="gray12" if ft < 0.5 else "gray25",
                    fill=_lerp(CYAN, BG, min(1.0, ft)))
        text, tone = self.model.banner()
        color = TONE_COLOR.get(tone, CYAN)
        self._final_born = self._final_born or now
        reveal = min(1.0, (now - self._final_born) / 0.5)
        grow = 34 + 5 * reveal
        glow_color = _lerp(BG, color, 0.35 * reveal)
        self.canvas.itemconfigure(
            self._final_glow, text=text, fill=glow_color, state="normal",
            font=self.F((self._font_banner[0], int(grow), "bold")))
        self.canvas.coords(self._final_glow, *self.P(BASE_W // 2 + 2, BASE_H // 2 - 8))
        self.canvas.itemconfigure(
            self._final_text, text=text, fill=color, state="normal",
            font=self.F((self._font_banner[0], int(grow), "bold")))
        self.canvas.coords(self._final_text, *self.P(BASE_W // 2, BASE_H // 2 - 10))
        self.canvas.tag_raise(self._final_glow)
        self.canvas.tag_raise(self._final_text)

    # ---------------------------------------------------------- état final
    def show_outcome(self, outcome: str, model: BootModel) -> None:
        self.render(model)
        self._start_transition()

    def show_failure(self, model: BootModel) -> None:
        self.render(model)
        self._hide_progress()
        c = self.canvas
        message = model.message or "Échec du démarrage."
        detail = (model.detail or "").strip().splitlines()
        tail = detail[-1][:120] if detail else ""
        for item, text, color in (
            (self._fail_items[0], "JARVIS START FAILED", ERR),
            (self._fail_items[1], f"COMPONENT : {model.failed_component()}", TEXT),
            (self._fail_items[2], f"ERROR : {message[:150]}", ERR),
            (self._fail_items[3],
             f"SUGGESTION : lance JARVIS_DOCTOR.command pour le diagnostic détaillé."
             + (f"  [{tail}]" if tail else ""), WARN),
        ):
            c.itemconfigure(item, text=text, fill=color, state="normal")
        self._draw_failure_buttons()

    def _hide_progress(self) -> None:
        for item in (getattr(self, "_bar_track", None), getattr(self, "_bar_fill", None),
                     getattr(self, "_bar_glow", None), getattr(self, "_bar_tick", None),
                     getattr(self, "_count_text", None), getattr(self, "_sub_text", None)):
            if item is not None:
                self.canvas.itemconfigure(item, state="hidden")
        self.canvas.itemconfigure(self._phase_text, state="hidden")

    def show_failure_message(self, text: str) -> None:
        if self.canvas is None:
            return
        self.canvas.create_text(*self.P(BASE_W // 2, BASE_H // 2), text=text, fill=ERR,
                                font=self.F(self._font_small), width=self.M(BASE_W - 120))
        self._draw_failure_buttons()

    def _draw_failure_buttons(self) -> None:
        if self._failure_drawn:
            return
        self._failure_drawn = True
        c = self.canvas
        cy = FOOTER_Y + 92
        bx1, bx2 = BASE_W - 340, BASE_W - 196
        cxx1, cxx2 = BASE_W - 180, BASE_W - 24
        c.create_rectangle(*self.P(bx1, cy - 16), *self.P(bx2, cy + 16),
                           outline=CYAN, fill=PANEL2, tags="doctor_btn")
        c.create_text(*self.P((bx1 + bx2) / 2, cy), text="OUVRIR LE DIAGNOSTIC", fill=CYAN,
                      font=self.F(self._font_small), tags="doctor_btn")
        c.create_rectangle(*self.P(cxx1, cy - 16), *self.P(cxx2, cy + 16),
                           outline=DIM, fill=PANEL2, tags="close_btn")
        c.create_text(*self.P((cxx1 + cxx2) / 2, cy), text="FERMER", fill=TEXT,
                      font=self.F(self._font_small), tags="close_btn")
        if self.controller is not None:
            c.tag_bind("doctor_btn", "<Button-1>",
                       lambda _e: self.controller.request_doctor())
        c.tag_bind("close_btn", "<Button-1>", lambda _e: self.close())
        c.tag_raise("doctor_btn")
        c.tag_raise("close_btn")

    def schedule_close(self, delay_s: float) -> None:
        self._close_after = self.root.after(int(max(0.0, delay_s) * 1000), self.close)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for after_id in (self._tick_after, self._drain_after, self._close_after):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ boucle
    def run(self) -> None:
        if animations_enabled():
            self._tick_after = self.root.after(TICK_MS, self._tick)
        self._drain_after = self.root.after(33, self._drain)
        self.root.mainloop()

    def _tick(self) -> None:
        if self._closing or self.canvas is None:
            return
        now = time.time()
        anim = animations_enabled()
        self._pulse += 0.10
        if anim:
            speed = 1.0
            if self._transition_start is not None:
                speed = 1.0 + 2.2 * min(1.0, (now - self._transition_start) / 1.5)
            self._angle[0] = (self._angle[0] + 0.6 * speed) % 360
            self._angle[1] = (self._angle[1] - 1.1 * speed) % 360
            self._angle[2] = (self._angle[2] + 2.4 * speed) % 360
            self._anim_loader(now)
            self._anim_ai_core(now)
            self._anim_background(now)
        self._render_transition(now)
        for item, born in self._activity_items:
            if animations_enabled() and now - born < 0.4:
                self.canvas.itemconfigure(item, fill=_lerp(PANEL, TEXT, (now - born) / 0.4))
        self.render(self.model)
        if self.model.finished:
            self._start_transition()
        self._tick_after = self.root.after(TICK_MS, self._tick)

    def _anim_background(self, now: float) -> None:
        y = HEADER_H + ((now * 55) % (FOOTER_Y - HEADER_H))
        self.canvas.coords(self._bg_scan, *self.P(0, y), *self.P(BASE_W, y))

    def _anim_loader(self, now: float) -> None:
        tone = self._tone
        base = TONE_COLOR.get(tone, CYAN)
        if tone == "err":
            base = ERR
        elif tone == "warn":
            base = WARN
        c = self.canvas
        for i, item in enumerate(self._ring_outer):
            c.itemconfigure(item, start=self._angle[0] + i * 120, extent=76,
                            outline=base)
        for i, item in enumerate(self._ring_mid):
            c.itemconfigure(item, start=self._angle[1] + i * 90, extent=54,
                            outline=_lerp(BG, base, 0.55))
        for i, item in enumerate(self._ring_seg):
            c.itemconfigure(item, start=self._angle[2] + i * 45, extent=14,
                            outline=_lerp(BG, base, 0.85))
        # Noyau qui respire.
        breath = 0.5 + 0.5 * math.sin(self._pulse * 0.9)
        r = 18 + 4 * breath
        cx, cy = LOADER_CX, LOADER_CY
        c.coords(self._core, *self.P(cx - r, cy - r), *self.P(cx + r, cy + r))
        c.itemconfigure(self._core, fill=_lerp(base, WHITE, 0.25 * breath))
        c.coords(self._core_ring, *self.P(cx - r - 7, cy - r - 7),
                 *self.P(cx + r + 7, cy + r + 7))
        c.itemconfigure(self._core_ring, outline=base)
        for i, glow in enumerate(self._core_glow):
            gr = 30 + i * 10 + 6 * breath
            c.coords(glow, *self.P(cx - gr, cy - gr), *self.P(cx + gr, cy + gr))
            c.itemconfigure(glow, fill=_lerp(PANEL2, base, 0.20 - i * 0.05))
        # Rayon de scan.
        ang = math.radians(self._angle[0] * 2)
        c.coords(self._scan_ray, *self.P(cx, cy),
                 *self.P(cx + LOADER_R * math.cos(ang), cy + LOADER_R * math.sin(ang)))
        c.itemconfigure(self._scan_ray, fill=_lerp(BG, base, 0.7))
        c.itemconfigure(self._loader_caption,
                        text="INITIALISATION" if not self.model.finished else "PRÊT")

    def _anim_ai_core(self, now: float) -> None:
        c = self.canvas
        for i, node in enumerate(self._nodes):
            r = 3 + 1.6 * (0.5 + 0.5 * math.sin(self._pulse * 1.4 + i))
            x, y = self._node_xy(i)
            c.coords(node, *self.P(x - r, y - r), *self.P(x + r, y + r))
            c.itemconfigure(node, fill=_lerp(CYAN_DIM, CYAN, 0.4 + 0.4 * math.sin(self._pulse + i)))
        for dot, x1, y1, x2, y2, phase in self._signals:
            t = (now * 0.6 + phase) % 1.0
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            c.coords(dot, *self.P(x - 2, y - 2), *self.P(x + 2, y + 2))

    def _node_xy(self, index: int) -> tuple[float, float]:
        return [(110, 452), (170, 440), (240, 452), (196, 492), (128, 512),
                (262, 512), (188, 466), (150, 478), (232, 478)][index]


def _console_fallback() -> int:
    """Si Tk est indisponible, on retombe sur le manager au premier plan."""
    try:
        return start(detach=False)
    except Exception as exc:
        print(f"JARVIS START FAILED : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--console" in argv:
        return _console_fallback()
    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception as exc:
        print(f"[boot] interface graphique indisponible ({exc}) — repli console.")
        return _console_fallback()

    model = BootModel()
    view = BootScreen(root, model)
    controller = BootController(view, model=model)
    view.bind_controller(controller)
    controller.start()
    try:
        view.run()
    except Exception as exc:
        print(f"[boot] {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            root.destroy()
        except Exception:
            pass
        return _console_fallback()
    return 0 if (controller.finished or model.outcome == "already_running") else 1


if __name__ == "__main__":
    raise SystemExit(main())
