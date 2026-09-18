"""Mission Control — vue temps réel de ce que JARVIS est en train de faire.

Principe de conception : ce module n'invente RIEN et ne pilote RIEN. Il
*observe* le bus d'événements existant et agrège, pour chaque tâche réelle,
l'état vivant que l'écran doit montrer : agent actif, état courant, étape en
cours, outil utilisé et ses paramètres, fichiers touchés, résultat, erreur,
durée, historique.

Pourquoi un observateur plutôt que des appels explicites partout : le backend
émet déjà tout ce qu'il faut (`task.*` porte le plan et les logs, `tool.*`
porte l'outil, l'agent et le `task_id`, `code.file.*` porte les fichiers). Tout
instrumenter à la main aurait dupliqué cette vérité à des dizaines d'endroits —
et la moindre omission aurait produit un écran qui ment. Ici, si un outil
tourne vraiment, il apparaît ; s'il n'apparaît pas, c'est qu'il n'a pas tourné.

Contrat avec le frontend (ui/js/v5/spatial_mission_control.js) :
    mission.started / mission.update / mission.completed / mission.failed
    GET /api/missions        → missions vivantes + historique récent
    GET /api/missions/<id>   → détail complet d'une mission
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .mission_store import STATUS_FILTERS, MissionStore

# États publics de la mission. Ce sont EXACTEMENT ceux affichés par l'UI.
STATES = ("WAITING", "THINKING", "RUNNING", "TOOL", "VERIFYING", "COMPLETED", "FAILED")
TERMINAL_STATES = ("COMPLETED", "FAILED")

# Clés dont la valeur ne doit jamais atteindre l'écran ni l'historique.
_SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey",
                "authorization", "auth", "credential", "private_key", "cookie")
_MAX_ARG_LEN = 220
_MAX_HISTORY = 120
_MAX_FILES = 40


def sanitize_args(arguments: Any) -> dict[str, Any]:
    """Réduit les arguments d'un outil à ce qui est montrable.

    Les secrets sont masqués, les gros contenus (fichier écrit, prompt) sont
    tronqués avec la taille réelle : l'opérateur doit voir QUE l'outil a reçu
    12 ko de contenu, pas les 12 ko.
    """
    if not isinstance(arguments, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in list(arguments.items())[:16]:
        name = str(key)
        low = name.lower()
        if any(s in low for s in _SECRET_KEYS):
            out[name] = "•••"
            continue
        if isinstance(value, str):
            out[name] = value if len(value) <= _MAX_ARG_LEN else f"{value[:_MAX_ARG_LEN]}… (+{len(value) - _MAX_ARG_LEN} car.)"
        elif isinstance(value, (int, float, bool)) or value is None:
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = f"[{len(value)} éléments]"
        elif isinstance(value, dict):
            out[name] = f"{{{len(value)} clés}}"
        else:
            out[name] = str(value)[:_MAX_ARG_LEN]
    return out


def _hms(seconds: Any) -> str:
    total = int(max(0.0, float(seconds or 0.0)))
    return "{:02d}:{:02d}:{:02d}".format(total // 3600, (total % 3600) // 60, total % 60)


def _offset(ms: Any) -> str:
    total = int(max(0, int(ms or 0)) / 1000)
    return "{:02d}:{:02d}".format(total // 60, total % 60)


def _stamp(ts: Any) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


class Mission:
    """Photographie vivante d'une tâche en cours."""

    __slots__ = ("id", "name", "kind", "agent", "state", "created_at", "started_at",
                 "ended_at", "progress", "steps", "tool", "last_tool", "files",
                 "result", "error", "tool_calls", "history", "conversation_id", "note",
                 "tools", "failed_step", "failed_tool", "_flushed")

    def __init__(self, mission_id: str, *, name: str = "", kind: str = "chat",
                 agent: str = "jarvis", conversation_id: str = "") -> None:
        now = time.time()
        self.id = mission_id
        self.name = name or "Mission"
        self.kind = kind
        self.agent = agent or "jarvis"
        self.conversation_id = conversation_id
        self.state = "WAITING"
        self.created_at = now
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.progress: float | None = None
        self.steps: list[dict[str, Any]] = []
        self.tool: dict[str, Any] | None = None       # outil en cours
        self.last_tool: dict[str, Any] | None = None  # dernier outil terminé
        self.files: list[dict[str, Any]] = []
        self.result = ""
        self.error = ""
        self.note = ""
        self.tool_calls = 0
        self.history: list[dict[str, Any]] = []
        self.tools: list[str] = []          # outils réellement appelés, dans l'ordre
        self.failed_step = ""               # étape en cours au moment de l'échec
        self.failed_tool = ""               # dernier outil avant l'échec
        self._flushed = 0                   # événements déjà persistés

    # -- mutations ---------------------------------------------------------
    def set_state(self, state: str) -> bool:
        if state not in STATES or state == self.state:
            return False
        # Une mission terminée ne repart pas : un événement tardif (log, outil
        # qui se referme) ne doit pas faire clignoter « TERMINÉ » en « EN COURS ».
        if self.state in TERMINAL_STATES:
            return False
        self.state = state
        if state not in TERMINAL_STATES and self.started_at is None:
            self.started_at = time.time()
        if state in TERMINAL_STATES:
            self.ended_at = time.time()
        return True

    def add_history(self, kind: str, label: str, **extra: Any) -> None:
        entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "label": str(label)[:300]}
        entry.update(extra)
        self.history.append(entry)
        if len(self.history) > _MAX_HISTORY:
            del self.history[: len(self.history) - _MAX_HISTORY]

    def touch_file(self, path: str, action: str) -> None:
        path = str(path or "").strip()
        if not path:
            return
        for entry in self.files:
            if entry["path"] == path:
                entry["action"] = action
                entry["ts"] = time.time()
                return
        self.files.append({"path": path[:300], "action": action, "ts": time.time()})
        if len(self.files) > _MAX_FILES:
            del self.files[0]

    # -- lecture -----------------------------------------------------------
    def duration(self) -> float:
        start = self.started_at or self.created_at
        return max(0.0, (self.ended_at or time.time()) - start)

    def step_counts(self) -> tuple[int, int]:
        done = sum(1 for s in self.steps if s.get("state") in ("done", "err"))
        return done, len(self.steps)

    def current_step(self) -> dict[str, Any] | None:
        for step in self.steps:
            if step.get("state") == "run":
                return step
        for step in reversed(self.steps):
            if step.get("state") in ("done", "err"):
                return step
        return None

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        done, total = self.step_counts()
        # La progression annoncée est celle que le backend a réellement
        # publiée ; à défaut, le ratio d'étapes RÉELLEMENT terminées. Jamais
        # une barre qui avance toute seule.
        progress = self.progress
        if progress is None and total:
            progress = done / total
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "agent": self.agent,
            "conversation_id": self.conversation_id,
            "state": self.state,
            "active": self.state not in TERMINAL_STATES,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": round(self.duration(), 2),
            "progress": progress,
            "steps": self.steps,
            "steps_done": done,
            "steps_total": total,
            "current_step": self.current_step(),
            "tool": self.tool,
            "last_tool": self.last_tool,
            "tool_calls": self.tool_calls,
            "tools": list(self.tools),
            "failed_step": self.failed_step,
            "failed_tool": self.failed_tool,
            "files": self.files[-12:],
            "result": self.result[:2000],
            "error": self.error[:1000],
            "note": self.note,
        }
        data["history"] = self.history if full else self.history[-12:]
        return data


class MissionControl:
    """Agrégateur temps réel branché sur le bus d'événements."""

    def __init__(self, events, db=None, *, max_live: int = 8, max_history: int = 30) -> None:
        self._events = events
        # La persistance est optionnelle : sans base, Mission Control reste
        # pleinement fonctionnel en direct, il perd seulement l'historique.
        self.store = MissionStore(db) if db is not None else None
        self._lock = threading.RLock()
        self._missions: dict[str, Mission] = {}
        self._order: list[str] = []
        self._current: str = ""
        self._max_live = max_live
        self._max_history = max_history
        self._last_update: dict[str, float] = {}
        self._pending: dict[str, threading.Timer] = {}
        self._subscribe()

    # -- branchement sur le bus -------------------------------------------
    def _subscribe(self) -> None:
        on = self._events.on
        on("task.created", self._on_task_created)
        on("task.started", self._on_task_started)
        on("task.progress", self._on_task_progress)
        on("task.waiting_confirmation", self._on_task_waiting)
        on("task.completed", self._on_task_completed)
        on("task.failed", self._on_task_failed)
        on("task.cancelled", self._on_task_cancelled)
        on("tool.called", self._on_tool_called)
        on("tool.completed", self._on_tool_finished)
        on("tool.failed", self._on_tool_finished)
        on("tool.denied", self._on_tool_denied)
        on("jarvis.state", self._on_jarvis_state)
        on("llm.started", self._on_thinking)
        on("verification.started", self._on_verifying)
        for evt in ("code.file.opened", "code.file.modified", "code.file.saved",
                    "code.file.created", "code.file.deleted"):
            on(evt, self._on_file)

    # -- helpers -----------------------------------------------------------
    def _get(self, mission_id: str) -> Mission | None:
        return self._missions.get(mission_id) if mission_id else None

    def _resolve(self, data: dict[str, Any]) -> Mission | None:
        """Retrouve la mission d'un événement.

        Les événements d'outil portent leur `task_id` : on l'utilise. Ceux qui
        n'en ont pas (états du cerveau, fichiers) sont rattachés à la mission
        active courante — et à rien du tout s'il n'y en a pas, plutôt que
        d'être collés arbitrairement à une mission déjà terminée.
        """
        mission = self._get(str(data.get("task_id") or data.get("id") or ""))
        if mission:
            return mission
        current = self._get(self._current)
        if current and current.state not in TERMINAL_STATES:
            return current
        return None

    def _register(self, mission: Mission) -> None:
        self._missions[mission.id] = mission
        self._order.append(mission.id)
        self._current = mission.id
        self._prune()

    def _prune(self) -> None:
        """Garde les missions vivantes + un historique court, borné en mémoire."""
        live = [mid for mid in self._order
                if (m := self._missions.get(mid)) and m.state not in TERMINAL_STATES]
        done = [mid for mid in self._order
                if (m := self._missions.get(mid)) and m.state in TERMINAL_STATES]
        keep = set(live[-self._max_live:]) | set(done[-self._max_history:])
        for mid in list(self._order):
            if mid not in keep:
                self._missions.pop(mid, None)
                self._last_update.pop(mid, None)
                self._cancel_pending(mid)
        self._order = [mid for mid in self._order if mid in keep]

    # Cadence maximale des `mission.update`. Un pipeline bavard (tri de la
    # boîte mail : un log par message) émettrait sinon des centaines de
    # rafraîchissements identiques vers tous les clients SSE. On coalesce, mais
    # SANS jamais perdre le dernier état : une mise à jour retardée est
    # reprogrammée, elle n'est pas jetée.
    _UPDATE_INTERVAL = 0.25

    def _publish(self, mission: Mission, event: str = "mission.update") -> None:
        # `cache=False` : ces mises à jour sont fréquentes et l'UI s'hydrate
        # par REST à l'ouverture. Les inonder dans l'historique rejoué au SSE
        # chasserait les événements utiles des autres écrans.
        if event != "mission.update":
            # Début et fin de mission : jamais retardés, jamais coalescés.
            self._cancel_pending(mission.id)
            self._events.emit(event, mission.to_dict(), cache=True)
            return
        now = time.time()
        last = self._last_update.get(mission.id, 0.0)
        if now - last >= self._UPDATE_INTERVAL:
            self._cancel_pending(mission.id)
            self._last_update[mission.id] = now
            self._events.emit(event, mission.to_dict(), cache=False)
            return
        if mission.id in self._pending:
            return  # une publication de queue est déjà programmée
        delay = self._UPDATE_INTERVAL - (now - last)
        timer = threading.Timer(delay, self._flush, args=(mission.id,))
        timer.daemon = True
        self._pending[mission.id] = timer
        timer.start()

    def _flush(self, mission_id: str) -> None:
        """Publie l'état final d'une rafale coalescée."""
        with self._lock:
            self._pending.pop(mission_id, None)
            mission = self._get(mission_id)
            if not mission:
                return
            self._last_update[mission_id] = time.time()
            payload = mission.to_dict()
        self._events.emit("mission.update", payload, cache=False)

    def _cancel_pending(self, mission_id: str) -> None:
        timer = self._pending.pop(mission_id, None)
        if timer:
            timer.cancel()

    # -- tâches ------------------------------------------------------------
    def _on_task_created(self, event: dict) -> None:
        data = event.get("data") or {}
        tid = str(data.get("id") or "")
        if not tid:
            return
        with self._lock:
            mission = self._get(tid)
            if mission:
                return
            mission = Mission(tid, name=str(data.get("name") or "Mission"),
                              kind=str(data.get("kind") or "chat"),
                              agent=str(data.get("agent") or "jarvis"),
                              conversation_id=str(data.get("conversation_id") or ""))
            mission.add_history("mission", "Mission créée")
            self._register(mission)
            self._publish(mission, "mission.started")
            self._persist(mission)

    def _on_task_started(self, event: dict) -> None:
        data = event.get("data") or {}
        with self._lock:
            mission = self._get(str(data.get("id") or ""))
            if not mission:
                self._on_task_created(event)
                mission = self._get(str(data.get("id") or ""))
            if not mission:
                return
            if data.get("agent"):
                mission.agent = str(data["agent"])
            if data.get("name"):
                mission.name = str(data["name"])
            self._current = mission.id
            mission.set_state("RUNNING")
            self._publish(mission)

    def _on_task_progress(self, event: dict) -> None:
        data = event.get("data") or {}
        with self._lock:
            mission = self._get(str(data.get("id") or ""))
            if not mission:
                return
            changed = False
            if isinstance(data.get("plan"), list):
                mission.steps = [
                    {"key": str(s.get("key", ""))[:60], "label": str(s.get("label", ""))[:80],
                     "state": str(s.get("state", "idle"))}
                    for s in data["plan"] if isinstance(s, dict)
                ]
                changed = True
            value = data.get("progress")
            if isinstance(value, (int, float)):
                mission.progress = max(0.0, min(1.0, float(value)))
                changed = True
            log = data.get("log")
            if isinstance(log, dict):
                level = str(log.get("level") or "info")
                message = str(log.get("message") or "")
                if message:
                    mission.add_history("log", message, level=level, data=log.get("data"))
                    changed = True
                # Une phase nommée par un pipeline (ex. audit de sécurité)
                # est l'étape courante la plus fiable qui soit : elle vient
                # du backend, pas d'une supposition de l'écran.
                payload = log.get("data")
                if isinstance(payload, dict):
                    if payload.get("phase"):
                        mission.note = str(payload["phase"])[:120]
                    total = payload.get("total")
                    completed = payload.get("completed")
                    if isinstance(total, (int, float)) and total and isinstance(completed, (int, float)):
                        mission.progress = max(0.0, min(1.0, float(completed) / float(total)))
            if data.get("message"):
                mission.add_history("log", str(data["message"]))
                changed = True
            if changed:
                self._publish(mission)
                self._persist(mission)

    def _on_task_waiting(self, event: dict) -> None:
        data = event.get("data") or {}
        with self._lock:
            mission = self._resolve(data)
            if not mission:
                return
            mission.set_state("WAITING")
            mission.note = str(data.get("action") or "Confirmation requise")[:120]
            mission.add_history("waiting", mission.note)
            self._publish(mission)

    def _finish(self, data: dict, state: str, *, result: str = "", error: str = "") -> None:
        with self._lock:
            mission = self._get(str(data.get("id") or ""))
            if not mission:
                return
            if isinstance(mission.steps, list):
                for step in mission.steps:
                    if step.get("state") == "run":
                        step["state"] = "done" if state == "COMPLETED" else "err"
            # `set_state` refuse les transitions depuis un état terminal ; on
            # force donc le verdict avant, sinon une tâche annulée puis
            # « échouée » garderait le premier verdict arrivé.
            mission.state = state
            mission.ended_at = time.time()
            if mission.started_at is None:
                mission.started_at = mission.created_at
            mission.tool = None
            if result:
                mission.result = str(result)
            if error:
                mission.error = str(error)
            if state == "COMPLETED":
                mission.progress = 1.0
            mission.add_history("mission", "Mission terminée" if state == "COMPLETED"
                                else (error or "Mission en échec"))
            if state == "FAILED":
                # Pour un diagnostic exploitable, on fige OU ca a lache.
                step = mission.current_step()
                mission.failed_step = str((step or {}).get("label") or mission.note or "")[:120]
                if not mission.failed_tool and mission.last_tool and not mission.last_tool.get("ok"):
                    mission.failed_tool = str(mission.last_tool.get("id") or "")
            if self._current == mission.id:
                self._current = ""
            self._persist(mission, final=True)
            self._prune()
            self._publish(mission, "mission.completed" if state == "COMPLETED" else "mission.failed")

    def _on_task_completed(self, event: dict) -> None:
        data = event.get("data") or {}
        self._finish(data, "COMPLETED", result=str(data.get("result") or ""))

    def _on_task_failed(self, event: dict) -> None:
        data = event.get("data") or {}
        self._finish(data, "FAILED", error=str(data.get("error") or "Échec sans détail."))

    def _on_task_cancelled(self, event: dict) -> None:
        data = event.get("data") or {}
        self._finish(data, "FAILED", error=str(data.get("error") or "Annulée."))

    # -- outils ------------------------------------------------------------
    def _on_tool_called(self, event: dict) -> None:
        data = event.get("data") or {}
        with self._lock:
            mission = self._resolve(data)
            if not mission or mission.state in TERMINAL_STATES:
                return
            tool_id = str(data.get("tool") or data.get("tool_id") or "")
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            if not args and data.get("path"):
                args = {"path": str(data["path"])}
            mission.tool_calls += 1
            if tool_id and tool_id not in mission.tools:
                mission.tools.append(tool_id)
            mission.tool = {
                "id": tool_id,
                "name": str(data.get("name") or tool_id),
                "args": sanitize_args(args),
                "risk": str(data.get("risk") or ""),
                "connector_id": str(data.get("connector_id") or ""),
                "started_at": time.time(),
            }
            if data.get("agent"):
                mission.agent = str(data["agent"])
            mission.set_state("TOOL")
            mission.add_history("tool", mission.tool["name"], tool=tool_id,
                                args=mission.tool["args"])
            if data.get("path"):
                mission.touch_file(str(data["path"]), "read")
            self._publish(mission)

    def _on_tool_finished(self, event: dict) -> None:
        data = event.get("data") or {}
        ok = bool(data.get("ok", event.get("type") == "tool.completed"))
        with self._lock:
            mission = self._resolve(data)
            if not mission or mission.state in TERMINAL_STATES:
                return
            tool_id = str(data.get("tool_id") or data.get("tool") or "")
            started = (mission.tool or {}).get("started_at")
            duration_ms = data.get("duration_ms")
            if not isinstance(duration_ms, (int, float)) and started:
                duration_ms = int((time.time() - started) * 1000)
            mission.last_tool = {
                "id": tool_id,
                "name": str(data.get("name") or tool_id),
                "args": (mission.tool or {}).get("args", {}),
                "ok": ok,
                "duration_ms": int(duration_ms or 0),
                "preview": str(data.get("preview") or "")[:600],
                "error": str(data.get("error") or "")[:600],
                "ended_at": time.time(),
            }
            mission.tool = None
            if not ok:
                mission.failed_tool = tool_id
            mission.add_history("tool_result", mission.last_tool["name"], ok=ok,
                                tool=tool_id, preview=mission.last_tool["preview"],
                                error=mission.last_tool["error"],
                                duration_ms=mission.last_tool["duration_ms"])
            # Un échec d'outil n'est pas un échec de mission : JARVIS peut
            # réessayer autrement. On l'affiche sans condamner la mission.
            mission.set_state("RUNNING")
            self._publish(mission)
            # Fin d'outil = point de reprise naturel. Persister ici, par
            # paquets, evite de perdre toute la timeline si JARVIS tombe au
            # milieu d'une mission longue.
            self._persist(mission)

    def _on_tool_denied(self, event: dict) -> None:
        data = event.get("data") or {}
        with self._lock:
            mission = self._resolve(data)
            if not mission or mission.state in TERMINAL_STATES:
                return
            mission.tool = None
            mission.add_history("denied", str(data.get("reason") or "Outil refusé."),
                                tool=str(data.get("tool") or ""))
            self._publish(mission)

    # -- états du cerveau ---------------------------------------------------
    _BRAIN_STATES = {
        "THINKING": "THINKING", "RECALLING": "THINKING", "LEARNING": "THINKING",
        "ACTING": "TOOL", "WAITING": "WAITING", "VERIFYING": "VERIFYING",
    }

    def _on_jarvis_state(self, event: dict) -> None:
        data = event.get("data") or {}
        mapped = self._BRAIN_STATES.get(str(data.get("state") or "").upper())
        if not mapped:
            return
        with self._lock:
            mission = self._get(self._current)
            if not mission or mission.state in TERMINAL_STATES:
                return
            # Un outil en cours prime : `jarvis.state` est plus grossier.
            if mission.tool and mapped != "WAITING":
                return
            if mission.set_state(mapped):
                self._publish(mission)

    def _on_thinking(self, event: dict) -> None:
        with self._lock:
            mission = self._resolve(event.get("data") or {})
            if mission and not mission.tool and mission.set_state("THINKING"):
                self._publish(mission)

    def _on_verifying(self, event: dict) -> None:
        with self._lock:
            mission = self._resolve(event.get("data") or {})
            if mission and mission.set_state("VERIFYING"):
                self._publish(mission)

    # -- fichiers ----------------------------------------------------------
    _FILE_ACTIONS = {
        "code.file.opened": "read", "code.file.modified": "modified",
        "code.file.saved": "saved", "code.file.created": "created",
        "code.file.deleted": "deleted",
    }

    def _on_file(self, event: dict) -> None:
        data = event.get("data") or {}
        path = str(data.get("path") or data.get("file") or "")
        if not path:
            return
        action = self._FILE_ACTIONS.get(str(event.get("type") or ""), "read")
        with self._lock:
            mission = self._resolve(data)
            if not mission or mission.state in TERMINAL_STATES:
                return
            mission.touch_file(path, action)
            self._publish(mission)

    # -- persistance --------------------------------------------------------
    _FLUSH_EVERY = 25

    def _persist(self, mission: Mission, *, final: bool = False) -> None:
        """Écrit la mission et la portion de timeline pas encore enregistrée.

        Écrire à chaque événement coûterait une transaction par ligne de log.
        On écrit donc par paquets — et systématiquement à la fin : un arrêt
        brutal perd au pire les toutes dernières lignes, jamais la mission.
        """
        if not self.store:
            return
        pending = mission.history[mission._flushed:]
        if not final and len(pending) < self._FLUSH_EVERY and mission._flushed:
            return
        try:
            self.store.save(mission.to_dict(full=True))
            if pending:
                written = self.store.append_events(
                    mission.id, pending, mission.started_at or mission.created_at)
                mission._flushed += written or len(pending)
        except Exception:
            # La supervision ne doit jamais faire tomber ce qu'elle observe.
            pass

    # -- lecture publique ---------------------------------------------------
    def history(self, status: str = "ALL", limit: int = 40, offset: int = 0) -> dict[str, Any]:
        """Historique consultable : missions actives, terminées, échouées.

        Les missions vivantes viennent de la mémoire (état à la seconde), les
        anciennes de la base. Une mission connue des deux côtés n'apparaît
        qu'une fois, dans sa version la plus fraîche.
        """
        status = (status or "ALL").upper()
        if status not in STATUS_FILTERS:
            status = "ALL"
        limit = max(1, min(200, int(limit)))
        with self._lock:
            live = [m.to_dict() for m in self._missions.values() if m.state not in TERMINAL_STATES]
            done = [m.to_dict() for m in self._missions.values() if m.state in TERMINAL_STATES]
        live.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        merged: list[dict[str, Any]] = []
        if status in ("ALL", "RUNNING") and offset == 0:
            merged.extend(live)
        seen = {m["id"] for m in merged}
        if self.store:
            for row in self.store.list(status, limit=limit, offset=offset):
                if row["id"] not in seen:
                    merged.append(row)
                    seen.add(row["id"])
        else:
            # Sans base : on rend ce que la mémoire sait, filtré de la même façon.
            done.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
            for row in done:
                if row["id"] not in seen and status in ("ALL", row["state"]):
                    merged.append(row)
                    seen.add(row["id"])
        if status in ("COMPLETED", "FAILED"):
            merged = [m for m in merged if m.get("state") == status]
        elif status == "RUNNING":
            merged = [m for m in merged if m.get("active")]
        counts = self.store.counts() if self.store else self._memory_counts()
        return {"missions": merged[:limit], "counts": counts,
                "status": status, "filters": list(STATUS_FILTERS)}

    def _memory_counts(self) -> dict[str, int]:
        with self._lock:
            states = [m.state for m in self._missions.values()]
        return {"ALL": len(states),
                "RUNNING": sum(1 for s in states if s not in TERMINAL_STATES),
                "COMPLETED": states.count("COMPLETED"),
                "FAILED": states.count("FAILED")}

    def timeline(self, mission_id: str) -> list[dict[str, Any]]:
        """Matière du mode REPLAY.

        Ce sont des événements DÉJÀ survenus : les rejouer ne réexécute rien,
        cela raconte seulement, dans l'ordre, ce qui a réellement eu lieu.
        """
        if self.store:
            persisted = self.store.timeline(mission_id)
            if persisted:
                return persisted
        with self._lock:
            mission = self._get(mission_id)
            if not mission:
                return []
            base = mission.started_at or mission.created_at
            return [{
                "ts": h.get("ts"),
                "offset_ms": int(max(0.0, (h.get("ts") or base) - base) * 1000),
                "kind": h.get("kind") or "log", "label": h.get("label") or "",
                "state": "", "tool": h.get("tool") or "",
                "ok": h.get("ok"), "duration_ms": int(h.get("duration_ms") or 0),
                "detail": str(h.get("error") or h.get("preview") or "")[:400],
            } for h in mission.history]

    def diagnostic(self, mission_id: str) -> str:
        """Résumé technique copiable d'une mission (surtout en échec).

        Fait pour être collé tel quel dans un ticket ou donné à un agent de
        développement : aucun secret, aucun gros payload — les arguments ont
        déjà été masqués par `sanitize_args` avant d'entrer ici.
        """
        mission = self.detail(mission_id)
        if not mission:
            return ""
        lines = [
            "=" * 52,
            "JARVIS — DIAGNOSTIC DE MISSION",
            "=" * 52,
            "",
            "MISSION   : " + str(mission.get("name") or "—"),
            "AGENT     : " + str(mission.get("agent") or "—"),
            "STATUT    : " + str(mission.get("state")),
            "DURATION  : " + _hms(mission.get("duration")),
        ]
        # Quand ça a échoué, STEP / TOOL / ERROR passent devant : c'est ce
        # qu'on lit en premier, et souvent tout ce qu'on lira.
        if mission.get("state") == "FAILED":
            lines += [
                "STEP      : " + str(mission.get("failed_step") or "—"),
                "TOOL      : " + str(mission.get("failed_tool") or "—"),
                "",
                "ERROR",
                "  " + str(mission.get("error") or "—"),
            ]
        else:
            step = mission.get("current_step") or {}
            lines.append("STEP      : " + str(step.get("label") or "—"))
            if mission.get("error"):
                lines += ["", "ERROR", "  " + str(mission["error"])]
        lines += [
            "",
            "CONTEXTE",
            "  Identifiant  : " + str(mission.get("id")),
            "  Début        : " + _stamp(mission.get("started_at") or mission.get("created_at")),
            "  Fin          : " + _stamp(mission.get("ended_at")),
            "  Étapes       : {}/{}".format(mission.get("steps_done", 0), mission.get("steps_total", 0)),
            "  Appels outil : {}".format(mission.get("tool_calls", 0)),
        ]
        tools = mission.get("tools") or []
        if tools:
            lines.append("  Outils       : " + ", ".join(str(t) for t in tools[:12]))
        steps = mission.get("steps") or []
        if steps:
            lines += ["", "STEPS"]
            glyphs = {"done": "✓", "run": "●", "err": "×", "idle": "○"}
            for i, step in enumerate(steps, 1):
                state = str(step.get("state", "idle"))
                lines.append("  {} {:02d}  {}".format(glyphs.get(state, "○"), i, step.get("label", "")))
        files = mission.get("files") or []
        if files:
            lines += ["", "FILES"]
            lines += ["  {:<9} {}".format(f.get("action", ""), f.get("path", "")) for f in files[:12]]
        timeline = mission.get("timeline") or self.timeline(mission_id)
        if timeline:
            # « LAST EVENTS » : les dernières actions avant le verdict. C'est
            # là qu'on voit ce qui a précédé l'échec.
            lines += ["", "LAST EVENTS"]
            for entry in timeline[-25:]:
                mark = "" if entry.get("ok") is None else ("  ok" if entry["ok"] else "  ÉCHEC")
                detail = str(entry.get("detail") or "")[:110]
                lines.append("  {}  {:<12} {}{}{}".format(
                    _offset(entry.get("offset_ms")), str(entry.get("kind", "")).upper(),
                    entry.get("label", ""), mark, ("  — " + detail) if detail else ""))
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            missions = [self._missions[mid] for mid in self._order if mid in self._missions]
            live = [m for m in missions if m.state not in TERMINAL_STATES]
            recent = [m for m in missions if m.state in TERMINAL_STATES]
            current = self._get(self._current) or (live[-1] if live else None)
            return {
                "current": current.to_dict(full=True) if current else None,
                "live": [m.to_dict() for m in reversed(live)],
                "recent": [m.to_dict() for m in reversed(recent[-self._max_history:])],
                "states": list(STATES),
            }

    def detail(self, mission_id: str) -> dict[str, Any] | None:
        """Mission vivante si elle est encore en mémoire, sinon sa version
        persistée : après un redémarrage, l'historique reste consultable."""
        with self._lock:
            mission = self._get(mission_id)
            if mission:
                data = mission.to_dict(full=True)
                data["timeline"] = self.timeline(mission_id)
                return data
        return self.store.detail(mission_id) if self.store else None


# ---------------------------------------------------------------------------
# Santé des composants
# ---------------------------------------------------------------------------
# Règle absolue de ce bloc : on n'écrit JAMAIS « ONLINE » sans l'avoir vérifié.
# Un composant sans sonde fiable est déclaré UNKNOWN. Un tableau de bord qui
# affiche du vert par défaut est pire que pas de tableau de bord : il fait
# chercher la panne partout sauf là où elle est.
ONLINE, OFFLINE, DEGRADED, UNKNOWN = "ONLINE", "OFFLINE", "DEGRADED", "UNKNOWN"


def _component(name: str, state: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    payload = {"id": name.lower().replace(" ", "_"), "name": name,
               "state": state if state in (ONLINE, OFFLINE, DEGRADED, UNKNOWN) else UNKNOWN,
               "detail": str(detail)[:160]}
    payload.update(extra)
    return payload


def health_report(core: Any) -> dict[str, Any]:
    """État réel des composants dont JARVIS dépend pour exécuter une mission."""
    components: list[dict[str, Any]] = []

    # CORE — le fait de répondre à cette requête EST la preuve qu'il tourne.
    try:
        uptime = max(0.0, time.time() - float(getattr(core, "started_at", 0) or 0))
        components.append(_component("JARVIS Core", ONLINE, _uptime(uptime), uptime=round(uptime, 1)))
    except Exception as exc:
        components.append(_component("JARVIS Core", DEGRADED, str(exc)))

    # LLM — sonde réseau réelle déjà tenue par LLMManager, avec son cache. Tant
    # qu'aucune sonde n'a abouti, le manager répond « Vérification… » : cela se
    # traduit par UNKNOWN, jamais par ONLINE.
    try:
        providers = core.llm.status() or []
        connected = [p for p in providers if p.get("connected")]
        configured = [p for p in providers if p.get("id")]
        if connected:
            names = ", ".join(str(p.get("name") or p.get("type")) for p in connected[:3])
            state, detail = ONLINE, names
        elif not configured:
            state, detail = OFFLINE, "Aucun fournisseur configuré"
        elif any("rification" in str(p.get("detail", "")) for p in providers):
            state, detail = UNKNOWN, "Vérification en cours"
        else:
            state, detail = OFFLINE, str(providers[0].get("detail") or "Aucun fournisseur joignable")
        components.append(_component("LLM", state, detail,
                                     providers=len(configured), connected=len(connected)))
    except Exception as exc:
        components.append(_component("LLM", UNKNOWN, f"Sonde impossible : {exc}"))

    # EVENT BUS — vérifiable : il expose ses abonnés et son historique.
    try:
        subscribers = int(core.events.subscriber_count())
        components.append(_component(
            "Event Bus", ONLINE,
            f"{subscribers} client(s) SSE" if subscribers else "Aucun client connecté",
            subscribers=subscribers))
    except Exception as exc:
        components.append(_component("Event Bus", DEGRADED, str(exc)))

    # TASK ENGINE — sonde réelle : on interroge ses compteurs.
    try:
        stats = core.tasks.stats() or {}
        active = int(stats.get("active") or 0)
        components.append(_component(
            "Task Engine", ONLINE,
            f"{active} active(s) · {stats.get('total', 0)} au total",
            active=active, total=int(stats.get("total") or 0)))
    except Exception as exc:
        components.append(_component("Task Engine", DEGRADED, str(exc)))

    # DATABASE — sonde réelle : une lecture qui doit répondre.
    try:
        started = time.time()
        core.db.scalar("SELECT 1")
        ms = int((time.time() - started) * 1000)
        components.append(_component("Database", ONLINE, f"lecture en {ms} ms", latency_ms=ms))
    except Exception as exc:
        components.append(_component("Database", OFFLINE, str(exc)))

    overall = ONLINE
    if any(c["state"] == OFFLINE for c in components):
        overall = OFFLINE
    elif any(c["state"] in (DEGRADED, UNKNOWN) for c in components):
        overall = DEGRADED
    return {"components": components, "overall": overall, "checked_at": time.time()}


def _uptime(seconds: float) -> str:
    total = int(seconds)
    if total >= 86400:
        return f"{total // 86400} j {(total % 86400) // 3600} h"
    if total >= 3600:
        return f"{total // 3600} h {(total % 3600) // 60} min"
    if total >= 60:
        return f"{total // 60} min"
    return f"{total} s"
