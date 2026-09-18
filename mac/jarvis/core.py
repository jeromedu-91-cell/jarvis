"""JARVIS Core — assemble tous les sous-systèmes et expose l'état réel."""
from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .build import IMAGE_PIPELINE_BUILD_ID, JARVIS_BUILD_ID
from .agents import AgentManager
from .audit import AuditLog
from .avatar import AvatarDirector
from .avatar_reference import AvatarReferenceManager
from .avatar_live import AvatarLiveManager
from .gpu_manager import GpuResourceManager
from .avatar_engine import AvatarEngine
from .avatar_update import AvatarUpdatePipeline
from .auto_learning import AutoLearning
from .idle_learning import IdleLearningEngine
from .automations import AutomationManager
from .blender import BlenderManager
from .brain_manager import BrainManager
from .browser_manager import BrowserManager, set_manager
from .crm import CrmStore
from .crm_pipeline import CrmPipeline
from .vault_credentials import AgentContext, CredentialVault, VaultDenied
from .attachments import AttachmentStore
from .calendar import CalendarManager
from .config import DATA_DIR, LEGACY_CONNECTIONS, SettingsStore, ensure_dirs
from .connectors import ConnectorManager
from .conversations import ConversationManager
from .db import Database
from .discord_call import MorningCallManager
from .discord_scheduler import DiscordScheduler
from .events import EventBus
from .document_store import DocumentStore
from .imagegen import ImageGenManager
from .llm import LLMManager
from .memory import MemoryManager
from .mission_control import MissionControl
from .monitor import SystemMonitor
from .orchestrator import Orchestrator
from .permissions import PermissionManager
from .secrets import SecretVault
from .tasks import TaskManager
from .tools import registry
from .tools.runner import SecureToolRunner
from .tts import PiperTTS
from .stt import SpeechRecognizer
from .tts_edge import EdgeTTS
from .voice import VoiceSessionManager, VoiceStateMachine
from .project_status import ProjectStatusService
from .validation import ValidationEngine

from .self_upgrade.service import SelfUpgradeService  # noqa: E402

# Enregistre les outils intégrés (import = enregistrement dans le registre).
from .tools import (avatar_engine_tools, avatar_tools, avatar_update_tools,  # noqa: F401,E402
                    blender_tools, browser_tools,
                    crm_tools, discord_tools, pdf_tools, transcript_tools,
                    image_tools,
                    file_analysis_tools,  # noqa: F401,E402
                    jarvis_tools,
                    remote_tools,
                    self_upgrade_tools, site_stats_tools,
                    system_tools, web_tools)  # noqa: F401,E402


class JarvisCore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        ensure_dirs()
        self.started_at = time.time()
        self.db = Database(db_path) if db_path else Database()
        self.settings = SettingsStore(self.db)

        self.events = EventBus(self.db, history=int(self.settings.get("developer", "event_history", 300)))
        self.documents = DocumentStore()
        self.vault = SecretVault(self.db)
        self.audit = AuditLog(self.db, vault=self.vault, settings=self.settings)
        self.connectors = ConnectorManager(self.db, self.vault, self.events, self.audit)
        self.permissions = PermissionManager(self.settings, self.audit)
        self.monitor = SystemMonitor(self.events, started_at=self.started_at)
        self.memory = MemoryManager(self.db, self.events, self.settings)
        self.conversations = ConversationManager(self.db, self.events, self.settings)
        self.tasks = TaskManager(self.db, self.events, self.settings)
        # Mission Control : observateur passif du bus d'événements — il ne
        # pilote rien, il agrège l'état réel des missions pour l'écran de
        # supervision (et persiste l'historique consultable après redémarrage).
        self.missions = MissionControl(self.events, db=self.db)
        self.calendar = CalendarManager(self.db, self.events)
        self.agents = AgentManager(self.db, self.events)
        self.llm = LLMManager(self.connectors, self.vault, self.settings, self.events)
        self.automations = AutomationManager(self.db, self.events, self.tasks, self.settings)
        # Planificateur Discord : dépend de `automations` (calcul du cron) et du
        # runner, donc construit après eux. Le moteur Discord, lui, reste créé à
        # la demande : planifier une tâche ne doit pas forcer la connexion du bot.
        self.discord_scheduler = DiscordScheduler(self)
        # Appel vocal matinal : même dépendance au moteur Discord, créé à la
        # demande. Le manager est construit même désactivé, pour que l'UI et les
        # outils puissent lire son statut et dire ce qui manque.
        self.discord_call = MorningCallManager(self)
        self.imagegen = ImageGenManager(self)
        self.blender = BlenderManager(self)
        self.avatar = AvatarDirector(self)
        self.avatar_ref = AvatarReferenceManager(self)
        self.avatar_pipeline = AvatarUpdatePipeline(self)
        self.avatar_engine = AvatarEngine(self)
        self.avatar_live = AvatarLiveManager(self)
        self.gpu = GpuResourceManager(self)
        self.runner = SecureToolRunner(self)
        self.validation = ValidationEngine()
        self.orchestrator = Orchestrator(self)
        self.voice = VoiceStateMachine(self.events)
        self.sessions = VoiceSessionManager(self.db, self.settings, self.events)
        self.tts = PiperTTS()
        # Repli quand aucune voix Piper n'est installée : sans lui, JARVIS
        # reste muet tant que l'utilisateur n'a pas téléchargé un modèle.
        self.tts_fallback = EdgeTTS()
        # Transcription locale (faster-whisper). Portée par le cœur parce que
        # deux appelants la partagent désormais : la dictée et l'appel vocal
        # Discord — deux instances chargeraient le modèle deux fois.
        self.stt = SpeechRecognizer(self.settings)
        self.registry = registry
        self.brain = BrainManager(self)
        self.project_status = ProjectStatusService(self)
        self.attachments = AttachmentStore(self)
        # CRM local : le carnet s'amorce au premier démarrage seulement, il
        # n'écrase jamais des contacts existants.
        self.crm = CrmStore(self.db)
        try:
            self.crm.seed_if_empty()
        except Exception:
            pass
        # CRM étendu (sociétés, opportunités, historique, scoring) et coffre-fort
        # d'identifiants. Le coffre réutilise `self.vault` : une seule
        # implémentation de chiffrement dans tout le système.
        self.crm_pipeline = CrmPipeline(self.db, events=self.events)
        self.credentials = CredentialVault(self.db, self.vault, audit=self.audit, events=self.events)
        try:
            self.credentials.sweep_expired()
        except Exception:
            pass
        self.browser = BrowserManager(self)
        set_manager(self.browser)
        self.auto_learning = AutoLearning(self)
        self.idle_learning = IdleLearningEngine(self)
        self.self_upgrade = SelfUpgradeService(self)
        self.activity = self.brain.activity
        self.active_task_context: dict[str, Any] = {"active_goal": "", "requested_file": "", "scope": "auto"}

        self._bind_avatar_events()
        self.memory.bind_core(self)
        self.automations.bind_core(self)

        self._stop = threading.Event()
        self._clap_listener = None
        self.clap_status = "désactivé"
        self.clap_device = ""
        self._ui_launched = False
        self._bootstrap()

    def _bind_avatar_events(self) -> None:
        """Le corps ne bouge que sur des faits : outils réellement exécutés."""
        def on_tool_started(event):
            data = event.get("data") or {}
            self.avatar.on_tool_started(str(data.get("tool_id") or data.get("tool") or ""),
                                        str(data.get("name") or ""))

        def on_tool_done(event):
            data = event.get("data") or {}
            self.avatar.on_tool_completed(str(data.get("tool_id") or data.get("tool") or ""),
                                          bool(data.get("ok", True)))

        self.events.on("tool.started", on_tool_started)
        self.events.on("tool.completed", on_tool_done)
        self.events.on("tool.failed", lambda e: self.avatar.on_tool_completed(
            str((e.get("data") or {}).get("tool_id") or ""), False))

    # -- démarrage ---------------------------------------------------------
    def _bootstrap(self) -> None:
        imported = 0
        try:
            imported = self.connectors.migrate_legacy(LEGACY_CONNECTIONS)
        except Exception as exc:
            self.events.feed("Migration des anciennes connexions impossible", level="warn",
                             kind="system", detail=str(exc)[:200], source="core")
        try:
            imported += self.connectors.bootstrap_from_env()
        except Exception:
            pass
        if imported:
            self.events.feed(f"{imported} connecteur(s) importés", level="info", kind="connector",
                             detail="Depuis connections.json et les variables d'environnement.", source="core")
        self.audit.record(action=f"JARVIS {__version__} démarré", tool="core",
                          detail={"vault": self.vault.backend, "tools": registry.count(),
                                  "build": JARVIS_BUILD_ID})
        try:
            self.attachments.cleanup()
        except Exception:
            pass
        print(f"[jarvis] JARVIS_BUILD_ID={JARVIS_BUILD_ID}", flush=True)
        # Plans de synchronisation prepares, indexes par empreinte.
        # Une application ne peut cibler qu'un plan deja prepare ici.
        self.sync_plans: dict[str, dict] = {}
        self.events.emit("system.ready", {"version": __version__, "tools": registry.count(),
                                          "build": JARVIS_BUILD_ID})

    def _clap_status_changed(self, status: str, device: str) -> None:
        self.clap_status, self.clap_device = status, device
        self.events.emit("clap.status", {"status": status, "device": device})
        print(f"Détecteur de claquements : {status} {device}", flush=True)

    def _on_double_clap(self, gap: float) -> None:
        if (self._stop.is_set() or self.voice.is_speaking
                or not self.settings.get("voice", "clap_enabled", True)):
            return
        self.events.emit("clap.detected", {"gap": gap})
        self.focus_ui(int(os.getenv("JARVIS_PORT", "8765")))

    def start_background(self) -> None:
        if (self.settings.get("voice", "clap_enabled", True)
                and os.getenv("JARVIS_CLAP_ENABLED", "1").lower() not in {"0", "false", "no"}
                and self._clap_listener is None):
            try:
                from .clap_listener import ClapListener
                self._clap_listener = ClapListener(self._on_double_clap, self._clap_status_changed)
                self._clap_listener.start()
            except Exception as exc:
                self._clap_status_changed(f"erreur: {exc}", "")
        self.monitor.start_broadcast(interval=5.0, stop_event=self._stop)
        self.idle_learning.start()
        self.automations.start_scheduler()
        self.discord_scheduler.start()
        self.discord_call.start()
        threading.Thread(target=self._maintenance_loop, daemon=True, name="jarvis-maintenance").start()
        # Sonde initiale des fournisseurs de modèles hors du chemin des requêtes.
        threading.Thread(target=self._llm_probe_loop, daemon=True, name="jarvis-llm-status").start()
        # Préchauffe la voix locale (chargement ~1,5 s) sans bloquer le boot.
        if self.settings.get("voice", "tts_provider", "browser") == "piper":
            threading.Thread(target=self.tts.warmup, daemon=True,
                             name="jarvis-tts-warmup").start()

    def _llm_probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.llm.status(max_age=0, blocking=True)
            except Exception:
                pass
            self._stop.wait(120)

    def _maintenance_loop(self) -> None:
        while not self._stop.wait(1800):
            try:
                self.events.prune()
                self.permissions.cleanup()
                self.audit.prune(int(self.settings.get("security", "audit_retention_days", 90)))
                self.tasks.prune(int(self.settings.get("automation", "task_retention_days", 30)))
                if self.missions.store:
                    self.missions.store.prune()
                self.sessions.prune()
                self.attachments.cleanup()
                self.llm.invalidate()
            except Exception:
                pass

    def shutdown(self) -> None:
        self._stop.set()
        if self._clap_listener is not None:
            self._clap_listener.stop()
        self.automations.stop()
        self.discord_scheduler.stop()
        self.discord_call.stop()
        self.idle_learning.stop()
        try:
            self.blender.shutdown()
        except Exception:
            pass
        try:
            self.avatar_live.shutdown()
        except Exception:
            pass
        try:
            self.audit.record(action="JARVIS arrêté", tool="core")
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    # -- état global (alimente le dashboard) --------------------------------
    def status(self) -> dict[str, Any]:
        metrics = self.monitor.snapshot()
        llm_status = self.llm.status()
        connectors = self.connectors.list()
        task_stats = self.tasks.stats()
        memory_stats = self.memory.stats()
        agents = self.agents.list()
        voice_settings = self.settings.section("voice")
        connected_llm = [p for p in llm_status if p["connected"]]

        return {
            "clap": {"status": self.clap_status, "device": self.clap_device},
            "version": __version__,
            "build_id": JARVIS_BUILD_ID,
            "image_pipeline_build_id": IMAGE_PIPELINE_BUILD_ID,
            "assistant_name": self.settings.get("general", "assistant_name", "JARVIS"),
            "user_name": self.settings.get("general", "user_name", "Jérôme"),
            "operator_title": self.settings.get("general", "operator_title", "Commander"),
            "uptime_s": int(time.time() - self.started_at),
            "started_at": self.started_at,
            "core": {
                "ai_core": {"status": "active" if connected_llm else "degraded",
                            "detail": f"{len(connected_llm)} fournisseur(s) connecté(s)"
                            if connected_llm else "Aucun fournisseur connecté"},
                "memory": {"status": "active", "detail": f"{memory_stats['total']} souvenirs",
                           "count": memory_stats["total"]},
                "voice": {"status": "online" if self.events.subscriber_count() else "idle",
                          "detail": self.voice.state, "state": self.voice.state},
                "agents": {"status": "running" if self.agents.running_count() else "standby",
                           "detail": f"{self.agents.running_count()} en cours",
                           "running": self.agents.running_count(), "total": len(agents)},
                "llms": {"status": "connected" if connected_llm else "not_configured",
                         "detail": f"{len(connected_llm)} connecté(s)", "count": len(connected_llm)},
                "system": {"status": self._system_health(metrics),
                           "detail": self._system_detail(metrics)},
            },
            "metrics": metrics,
            "agents": agents,
            "tasks": task_stats,
            "memory": memory_stats,
            "conversations": self.conversations.stats(),
            "llm": llm_status,
            "connectors": {
                "total": len(connectors),
                "connected": len([c for c in connectors if c["status"] == "connected"]),
                "error": len([c for c in connectors if c["status"] == "error"]),
                "items": connectors,
            },
            "tools": {"total": registry.count(), "enabled": len([t for t in registry.all() if t.enabled]),
                      "categories": registry.categories()},
            "automations": {"total": len(self.automations.list()),
                            "enabled": len(self.automations.list(enabled_only=True))},
            "voice": {
                **self.voice.snapshot(),
                "mode": voice_settings.get("mode"),
                "wake_word": voice_settings.get("wake_word"),
                "greeting_enabled": voice_settings.get("greeting_enabled"),
                "greeting_frequency": voice_settings.get("greeting_frequency"),
                "sessions": self.sessions.stats(),
            },
            "security": {"vault_backend": self.vault.backend, "secrets": self.vault.count(),
                         "audit_entries": self.audit.count(),
                         "pending_confirmations": self.permissions.pending_list()},
            "brain": self.brain.stats(),
            "learning": self.idle_learning.status(),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "data_dir": str(DATA_DIR),
                "opencode": shutil.which("opencode") is not None,
                "cursor": self._app_exists("Cursor") or shutil.which("cursor") is not None,
                "chrome": self._app_exists("Google Chrome"),
                "docker": shutil.which("docker") is not None,
                "blender": self.blender.available(),
                "rsync": shutil.which("rsync") is not None,
            },
            "location": self.settings.get("general", "location", ""),
            "ts": time.time(),
        }

    @staticmethod
    def _system_detail(metrics: dict[str, Any]) -> str:
        """Résumé factuel : une métrique absente est signalée, jamais inventée."""
        parts = []
        for key, label in (("cpu", "CPU"), ("memory", "RAM"), ("disk", "Disque")):
            value = metrics[key]["percent"]
            parts.append(f"{label} {value}%" if value is not None else f"{label} —")
        net = {"excellent": "réseau OK", "correct": "réseau OK", "offline": "hors ligne",
               "unknown": "réseau en test"}.get(metrics["network"]["status"], "")
        if net:
            parts.append(net)
        return " · ".join(parts)

    @staticmethod
    def _system_health(metrics: dict[str, Any]) -> str:
        values = [metrics["cpu"]["percent"], metrics["memory"]["percent"], metrics["disk"]["percent"]]
        values = [v for v in values if v is not None]
        if not values:
            return "unknown"
        if max(values) >= 92 or metrics["network"]["status"] == "offline":
            return "warning"
        return "optimal"

    @staticmethod
    def _app_exists(name: str) -> bool:
        if platform.system() == "Windows":
            from .windows import find_app
            return find_app(name) is not None
        for base in (Path("/Applications"), Path.home() / "Applications"):
            if (base / f"{name}.app").exists():
                return True
        return False

    # -- interface ---------------------------------------------------------
    def launch_ui(self, port: int) -> None:
        if self._ui_launched or not self.settings.get("general", "launch_ui_on_start", True):
            return
        if os.getenv("JARVIS_LAUNCH_UI", "1").strip().lower() in {"0", "false", "no"}:
            self._ui_launched = True
            return
        url = f"http://127.0.0.1:{port}/"
        self._ui_launched = True
        try:
            import subprocess

            if platform.system() == "Windows":
                from .windows import launch_ui
                if launch_ui(url):
                    return
            if platform.system() == "Darwin" and self._app_exists("Google Chrome"):
                subprocess.Popen(
                    ["open", "-na", "Google Chrome", "--args", f"--app={url}",
                     "--new-window", "--window-size=1680,1050"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    def focus_ui(self, port: int) -> None:
        url = f"http://127.0.0.1:{int(port)}/"
        if platform.system() == "Darwin" and self._app_exists("Google Chrome"):
            import subprocess
            script = (f'tell application "Google Chrome"\n activate\n'
                      ' repeat with w in windows\n'
                      ' repeat with i from 1 to count of tabs of w\n'
                      f' if URL of tab i of w starts with "{url}" then\n'
                      ' set active tab index of w to i\n set minimized of w to false\n'
                      ' set index of w to 1\n return\n end if\n'
                      ' end repeat\n end repeat\n'
                      f' open location "{url}"\nend tell')
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        else:
            import webbrowser
            webbrowser.open(url)
