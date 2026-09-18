"""Event Bus : diffusion temps réel vers les clients SSE + persistance du feed."""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .db import Database, dumps

# Types d'événements officiels (contrat avec le frontend).
EVENT_TYPES = {
    "agent.started", "agent.progress", "agent.completed", "agent.failed", "agent.idle",
    "task.created", "task.started", "task.progress", "task.completed", "task.failed",
    "task.cancelled", "task.waiting_confirmation",
    "tool.called", "tool.completed", "tool.failed", "tool.denied",
    "connector.connected", "connector.failed", "connector.updated", "connector.deleted",
    "voice.state", "voice.transcript", "voice.speaking", "voice.error",
    "memory.created", "memory.updated", "memory.deleted",
    "conversation.created", "conversation.message", "conversation.updated", "conversation.deleted",
    "workflow.created", "workflow.started", "workflow.completed", "workflow.failed", "workflow.updated",
    "system.metrics", "system.warning", "system.info", "system.ready",
    "llm.status", "feed.new", "settings.updated", "calendar.updated",
    # JARVIS 4 — cerveau vivant, états, activité temps réel.
    "jarvis.state", "jarvis.activity", "jarvis.tool.notice",
    "activity.trace",
    # Analyse Google Sheet : progression reelle du pipeline (aucune etape simulee).
    "sheet.progress",
    "brain.search", "brain.node.selected", "brain.path", "brain.tool.active",
    "brain.learn.created", "brain.learn.updated",
    "knowledge.learn.created", "knowledge.learn.updated",
    "knowledge.validated", "knowledge.failed",
    "tts.started", "tts.audio_level", "tts.completed",
    "speech.listening.started", "speech.listening.stopped",
    "llm.started", "llm.completed", "llm.delta",
    "memory.search.started", "memory.search.result",
    "knowledge.search.started", "knowledge.node.selected",
    "tool.started", "tool.completed", "tool.failed",
    "learning.idle.detected", "learning.session.started", "learning.topic.selected",
    "learning.search.started", "learning.source.read", "learning.knowledge.created",
    "learning.knowledge.updated", "learning.conflict.detected", "learning.session.completed",
    "learning.paused_by_user", "learning.queue.auto_created",
    "code.file.opening", "code.file.opened", "code.file.modified", "code.file.saving",
    "code.file.saved", "code.file.created", "code.file.deleted", "code.file.conflict", "code.file.error",
    # Génération d'image (contrat temps réel avec GeneratingImageMessage).
    "image.generation.started", "image.generation.queued", "image.generation.progress",
    "image.generation.preview", "image.generation.completed", "image.generation.failed",
    # Atelier 3D Blender (contrat temps réel avec ModelMessages / viewer GLB).
    "blender.job.started", "blender.job.queued", "blender.job.progress",
    "blender.job.completed", "blender.job.failed", "blender.job.cancelled",
    "blender.geometry.started", "blender.geometry.completed",
    "blender.materials.completed", "blender.textures.completed",
    "blender.rig.completed", "blender.animation.completed",
    "blender.optimize.completed",
    "blender.render.started", "blender.render.completed",
    "blender.export.completed", "blender.preview.ready",
    # Avatar Studio live — contrat temps reel avec le viewer 3D et la timeline.
    "avatar.job.started", "avatar.job.progress", "avatar.job.completed",
    "avatar.job.failed", "avatar.job.cancelled", "avatar.job.paused",
    "avatar.job.resumed", "avatar.job.stalled", "avatar.job.warning",
    "avatar.job.accepted", "avatar.job.rejected", "avatar.job.analysis_failed",
    "avatar.stage.started", "avatar.stage.progress", "avatar.stage.completed",
    "avatar.stage.skipped", "avatar.operation",
    "avatar.preview.glb", "avatar.preview.render",
    "avatar.iteration.started", "avatar.iteration.completed",
    "avatar.evaluation.completed",
    "avatar.adjustment.queued", "avatar.adjustment.applied",
    "avatar.update_started", "avatar.update_progress", "avatar.update_completed",
    "avatar.update_failed", "avatar.revision_activated",
    # Corps de JARVIS — contrat temps réel avec l'avatar 3D.
    "avatar.state", "avatar.gesture", "avatar.move_requested", "avatar.look",
    "avatar.view", "avatar.ready", "avatar.spawn",
    "tts.viseme",
    "ssh.connected",
    "n8n.workflow.created",
    "coding.started", "coding.completed",
    "deploy.started", "deploy.completed",
    "verification.started", "verification.completed",
    # Mission Control V1 — contrat temps réel avec spatial_mission_control.js.
    "mission.started", "mission.update", "mission.completed", "mission.failed",
    # Self Upgrade V1 — contrat temps réel avec la page Self Upgrades.
    "upgrade.started", "upgrade.progress", "upgrade.log", "upgrade.plan_ready",
    "upgrade.workspace_ready", "upgrade.building", "upgrade.testing", "upgrade.candidate",
    "upgrade.promoting", "upgrade.installed", "upgrade.completed", "upgrade.failed",
    "upgrade.rolled_back", "upgrade.cancelled",
    # Tri de la boîte mail — contrat temps réel avec le Kanban du Command
    # Center. `mail.message.classified` est émis au fil du tri, pas en bloc.
    "mail.inbox.started", "mail.message.classified", "mail.inbox.completed",
    "mail.inbox.failed",
    # CRM & documents — levée d'ambiguïté et aperçu du PDF généré.
    "crm.clarification.needed", "crm.contact.saved", "document.generated",
    "transcript.parsed",
}

LEVELS = ("info", "warn", "error", "live", "tip", "meeting", "focus", "overdue")


class EventBus:
    def __init__(self, db: Database, history: int = 300) -> None:
        self._db = db
        self._subscribers: dict[int, queue.Queue] = {}
        self._next_id = 1
        self._lock = threading.RLock()
        self._history: list[dict[str, Any]] = []
        self._history_max = history
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}

    # -- abonnements SSE ---------------------------------------------------
    def subscribe(self) -> tuple[int, queue.Queue]:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subscribers[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subscribers.pop(sid, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- listeners internes (in-process) -----------------------------------
    def on(self, event_type: str, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.setdefault(event_type, []).append(callback)

    # -- émission ----------------------------------------------------------
    def emit(self, event_type: str, payload: dict[str, Any] | None = None, *,
             persist: bool = False, cache: bool = True) -> dict[str, Any]:
        """Émet un événement.

        `cache=False` (flux haute fréquence comme les frames navigateur) :
        distribué aux abonnés mais exclu de l'historique rejoué au SSE.
        """
        event = {"type": event_type, "ts": time.time(), "data": payload or {}}
        with self._lock:
            if cache:
                self._history.append(event)
            if len(self._history) > self._history_max:
                del self._history[: len(self._history) - self._history_max]
            subs = list(self._subscribers.items())
            listeners = list(self._listeners.get(event_type, [])) + list(self._listeners.get("*", []))
        for sid, q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Client trop lent : on le libère plutôt que de bloquer le core.
                self.unsubscribe(sid)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                pass
        if persist:
            try:
                self._db.execute(
                    "INSERT INTO events(ts, type, payload) VALUES(?,?,?)",
                    (event["ts"], event_type, dumps(event["data"])),
                )
            except Exception:
                pass
        return event

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])

    # -- Live Intelligence Feed --------------------------------------------
    def feed(
        self,
        title: str,
        *,
        level: str = "info",
        kind: str = "system",
        detail: str = "",
        source: str = "jarvis",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        level = level if level in LEVELS else "info"
        ts = time.time()
        cur = self._db.execute(
            "INSERT INTO feed(ts, level, kind, title, detail, source, meta, read) VALUES(?,?,?,?,?,?,?,0)",
            (ts, level, kind, title[:400], detail[:2000], source, dumps(meta or {})),
        )
        item = {
            "id": cur.lastrowid,
            "ts": ts,
            "level": level,
            "kind": kind,
            "title": title[:400],
            "detail": detail[:2000],
            "source": source,
            "meta": meta or {},
            "read": 0,
        }
        self.emit("feed.new", item)
        return item

    def feed_items(self, limit: int = 40, unread_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM feed"
        if unread_only:
            sql += " WHERE read=0"
        sql += " ORDER BY ts DESC LIMIT ?"
        from .db import loads

        rows = self._db.query(sql, (limit,))
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["meta"] = loads(d.get("meta"), {})
            out.append(d)
        return out

    def mark_feed_read(self, item_id: int | None = None) -> None:
        if item_id is None:
            self._db.execute("UPDATE feed SET read=1 WHERE read=0")
        else:
            self._db.execute("UPDATE feed SET read=1 WHERE id=?", (item_id,))

    def prune(self, keep_feed: int = 500, keep_events: int = 2000) -> None:
        self._db.execute(
            "DELETE FROM feed WHERE id NOT IN (SELECT id FROM feed ORDER BY ts DESC LIMIT ?)", (keep_feed,)
        )
        self._db.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY ts DESC LIMIT ?)", (keep_events,)
        )
