"""Persistance de Mission Control — l'historique survit au redémarrage.

Ce que l'on garde : la SUPERVISION d'une mission (qui, quoi, combien de temps,
avec quels outils, quel verdict) et une timeline compacte qui permet de rejouer
visuellement la mission après coup.

Ce que l'on ne garde PAS : les gros payloads. Le contenu d'un fichier écrit, la
sortie complète d'un outil ou un prompt de 12 ko n'ont rien à faire dans un
historique de supervision — on stocke un aperçu tronqué, qui suffit à
comprendre ce qui s'est passé, et les secrets sont déjà masqués en amont par
`mission_control.sanitize_args`.

La table `tasks` n'est pas dupliquée : elle porte l'état métier de la tâche,
`missions` porte la lecture opérateur (agent actif, étape d'échec, outils).
"""
from __future__ import annotations

import time
from typing import Any

from .db import dumps, loads

# Bornes de stockage. Une mission bavarde (tri de boîte mail) ne doit pas
# écrire des dizaines de milliers de lignes pour une seule exécution.
_MAX_EVENTS_PER_MISSION = 400
_PREVIEW = 400
_ERROR_MAX = 1200
_RESULT_MAX = 2000

STATUS_FILTERS = ("ALL", "RUNNING", "COMPLETED", "FAILED")


class MissionStore:
    """Écriture et relecture des missions persistées."""

    def __init__(self, db) -> None:
        self._db = db
        self._recover()

    # -- redémarrage --------------------------------------------------------
    def _recover(self) -> None:
        """Aucune mission ne reste « en cours » après un redémarrage.

        Mentir ici serait pire que de perdre la ligne : un opérateur verrait
        une mission éternellement RUNNING, sans rien derrière pour l'exécuter.
        On dit donc ce qui s'est réellement passé — elle a été interrompue.
        """
        try:
            self._db.execute(
                "UPDATE missions SET status='FAILED', error=?, completed_at=COALESCE(completed_at, ?) "
                "WHERE status NOT IN ('COMPLETED','FAILED')",
                ("Interrompue par le redémarrage de JARVIS.", time.time()),
            )
        except Exception:
            # Une base indisponible ne doit jamais empêcher JARVIS de démarrer :
            # Mission Control retombe sur son suivi en mémoire seule.
            pass

    # -- écriture -----------------------------------------------------------
    def save(self, mission: dict[str, Any]) -> None:
        """Enregistre ou met à jour une mission (UPSERT idempotent)."""
        try:
            self._db.execute(
                "INSERT INTO missions(id, name, kind, agent, status, conversation_id, created_at, "
                "started_at, completed_at, duration, tools, tool_calls, steps_total, steps_done, "
                "steps, files, error, result, failed_step, failed_tool, note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, agent=excluded.agent, "
                "status=excluded.status, completed_at=excluded.completed_at, duration=excluded.duration, "
                "tools=excluded.tools, tool_calls=excluded.tool_calls, steps_total=excluded.steps_total, "
                "steps_done=excluded.steps_done, steps=excluded.steps, files=excluded.files, "
                "error=excluded.error, result=excluded.result, failed_step=excluded.failed_step, "
                "failed_tool=excluded.failed_tool, note=excluded.note",
                (
                    mission["id"], str(mission.get("name") or "")[:300], mission.get("kind") or "chat",
                    mission.get("agent") or "jarvis", mission.get("state") or "RUNNING",
                    mission.get("conversation_id") or "", mission.get("created_at"),
                    mission.get("started_at"), mission.get("ended_at"),
                    float(mission.get("duration") or 0.0),
                    dumps(mission.get("tools") or []), int(mission.get("tool_calls") or 0),
                    int(mission.get("steps_total") or 0), int(mission.get("steps_done") or 0),
                    dumps(mission.get("steps") or []), dumps(mission.get("files") or []),
                    str(mission.get("error") or "")[:_ERROR_MAX],
                    str(mission.get("result") or "")[:_RESULT_MAX],
                    str(mission.get("failed_step") or "")[:120],
                    str(mission.get("failed_tool") or "")[:120],
                    str(mission.get("note") or "")[:200],
                ),
            )
        except Exception:
            pass

    def append_events(self, mission_id: str, entries: list[dict[str, Any]], started_at: float) -> int:
        """Ajoute des événements à la timeline rejouable. Renvoie le nombre écrit."""
        if not entries:
            return 0
        try:
            written = int(self._db.scalar(
                "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)) or 0)
        except Exception:
            return 0
        room = max(0, _MAX_EVENTS_PER_MISSION - written)
        if not room:
            return 0
        count = 0
        for entry in entries[:room]:
            ts = float(entry.get("ts") or time.time())
            # L'offset est ce dont le REPLAY a besoin : le temps ECOULE depuis
            # le debut, pas une horloge absolue qui ne veut plus rien dire
            # quand on relit la mission trois jours plus tard.
            offset = int(max(0.0, ts - (started_at or ts)) * 1000)
            detail = entry.get("error") or entry.get("preview") or entry.get("detail") or ""
            if not detail and entry.get("args"):
                detail = dumps(entry["args"])
            ok = entry.get("ok")
            try:
                self._db.execute(
                    "INSERT INTO mission_events(mission_id, ts, offset_ms, kind, label, state, tool, "
                    "ok, duration_ms, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (mission_id, ts, offset, str(entry.get("kind") or "log")[:40],
                     str(entry.get("label") or "")[:300], str(entry.get("state") or "")[:20],
                     str(entry.get("tool") or "")[:120],
                     None if ok is None else (1 if ok else 0),
                     int(entry.get("duration_ms") or 0), str(detail)[:_PREVIEW]),
                )
                count += 1
            except Exception:
                break
        return count

    # -- lecture ------------------------------------------------------------
    def list(self, status: str = "ALL", limit: int = 40, offset: int = 0) -> list[dict[str, Any]]:
        status = (status or "ALL").upper()
        sql = "SELECT * FROM missions"
        params: list[Any] = []
        if status == "RUNNING":
            # « RUNNING » côté filtre = tout ce qui n'est pas termine : une
            # mission en attente de confirmation est bien encore en cours.
            sql += " WHERE status NOT IN ('COMPLETED','FAILED')"
        elif status in ("COMPLETED", "FAILED"):
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(200, int(limit))), max(0, int(offset))])
        try:
            return [self._row(r) for r in self._db.query(sql, params)]
        except Exception:
            return []

    def counts(self) -> dict[str, int]:
        out = {"ALL": 0, "RUNNING": 0, "COMPLETED": 0, "FAILED": 0}
        try:
            for row in self._db.query("SELECT status, COUNT(*) AS n FROM missions GROUP BY status"):
                status = str(row["status"] or "").upper()
                out["ALL"] += row["n"]
                if status in ("COMPLETED", "FAILED"):
                    out[status] += row["n"]
                else:
                    out["RUNNING"] += row["n"]
        except Exception:
            pass
        return out

    def get(self, mission_id: str) -> dict[str, Any] | None:
        try:
            row = self._db.one("SELECT * FROM missions WHERE id=?", (mission_id,))
        except Exception:
            return None
        return self._row(row) if row else None

    def timeline(self, mission_id: str, limit: int = _MAX_EVENTS_PER_MISSION) -> list[dict[str, Any]]:
        """Événements réels, dans l'ordre chronologique — la matière du REPLAY."""
        try:
            rows = self._db.query(
                "SELECT ts, offset_ms, kind, label, state, tool, ok, duration_ms, detail "
                "FROM mission_events WHERE mission_id=? ORDER BY id LIMIT ?", (mission_id, int(limit)))
        except Exception:
            return []
        out = []
        for r in rows:
            out.append({"ts": r["ts"], "offset_ms": r["offset_ms"], "kind": r["kind"],
                        "label": r["label"], "state": r["state"], "tool": r["tool"],
                        "ok": None if r["ok"] is None else bool(r["ok"]),
                        "duration_ms": r["duration_ms"], "detail": r["detail"]})
        return out

    def detail(self, mission_id: str) -> dict[str, Any] | None:
        mission = self.get(mission_id)
        if not mission:
            return None
        mission["timeline"] = self.timeline(mission_id)
        return mission

    # -- entretien ----------------------------------------------------------
    def prune(self, keep: int = 500) -> int:
        """Borne l'historique : sans cela, la base grossit indéfiniment."""
        try:
            stale = [r["id"] for r in self._db.query(
                "SELECT id FROM missions WHERE id NOT IN "
                "(SELECT id FROM missions ORDER BY created_at DESC LIMIT ?)", (int(keep),))]
            for mid in stale:
                self._db.execute("DELETE FROM mission_events WHERE mission_id=?", (mid,))
            if stale:
                self._db.execute(
                    "DELETE FROM missions WHERE id NOT IN "
                    "(SELECT id FROM missions ORDER BY created_at DESC LIMIT ?)", (int(keep),))
            return len(stale)
        except Exception:
            return 0

    @staticmethod
    def _row(row) -> dict[str, Any]:
        d = {k: row[k] for k in row.keys()}
        d["tools"] = loads(d.get("tools"), []) or []
        d["steps"] = loads(d.get("steps"), []) or []
        d["files"] = loads(d.get("files"), []) or []
        d["state"] = d.get("status") or "RUNNING"
        d["active"] = d["state"] not in ("COMPLETED", "FAILED")
        d["ended_at"] = d.get("completed_at")
        d["persisted"] = True
        return d
