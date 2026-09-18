"""Serveur HTTP local : API JSON + flux d'événements SSE + fichiers statiques.

Écoute uniquement sur 127.0.0.1. Aucun secret ne transite par l'API.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__
from .vault_credentials import ALL_SECRET_FIELDS, AgentContext, VaultDenied
from .attachments import AttachmentError
from .config import UI_DIR
from .connectors import type_catalog
from .discord_scheduler import ScheduleError
from .permissions import RISK_LABELS
from .tools.base import registry


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler))

    def get(self, pattern: str):
        def deco(fn):
            self.add("GET", pattern, fn)
            return fn
        return deco

    def post(self, pattern: str):
        def deco(fn):
            self.add("POST", pattern, fn)
            return fn
        return deco

    def put(self, pattern: str):
        def deco(fn):
            self.add("PUT", pattern, fn)
            return fn
        return deco

    def delete(self, pattern: str):
        def deco(fn):
            self.add("DELETE", pattern, fn)
            return fn
        return deco

    def match(self, method: str, path: str):
        for m, regex, handler in self.routes:
            if m != method.upper():
                continue
            found = regex.match(path)
            if found:
                return handler, found.groupdict()
        return None, {}


router = Router()
CORE = None  # injecté par create_server

# Extensions traitées comme des assets : un 404 vaut mieux qu'un index.html.
ASSET_SUFFIXES = {".js", ".mjs", ".css", ".glb", ".gltf", ".bin", ".png", ".jpg",
                  ".jpeg", ".svg", ".webp", ".wav", ".mp3", ".ogg", ".json",
                  ".woff", ".woff2", ".ttf", ".map"}


def _ok(data: Any = None, **extra) -> dict[str, Any]:
    out = {"ok": True}
    if isinstance(data, dict):
        out.update(data)
    elif data is not None:
        out["data"] = data
    out.update(extra)
    return out


def _err(message: str, status: int = 400) -> tuple[dict[str, Any], int]:
    return {"ok": False, "error": message}, status


class RawResponse:
    """Réponse HTTP non-JSON (WAV, binaire…) renvoyée par un endpoint."""

    def __init__(self, body: bytes, content_type: str = "application/octet-stream",
                 status: int = 200) -> None:
        self.body = body
        self.content_type = content_type
        self.status = status


# ---------------------------------------------------------------------------
# État & système
# ---------------------------------------------------------------------------
@router.get("/api/status")
def api_status(req):
    return _ok(CORE.status())


@router.get("/api/metrics")
def api_metrics(req):
    return _ok({"metrics": CORE.monitor.snapshot(max_age=1.0)})


@router.post("/api/code/rendered")
def api_code_rendered(req):
    return _ok({"accepted": CORE.documents.acknowledge(req["body"])})


@router.get("/api/code/documents")
def api_code_documents(req):
    return _ok(CORE.documents.snapshot())


@router.post("/api/code/close")
def api_code_close(req):
    CORE.documents.close(req['body'].get('document_id'))
    return _ok()


@router.get("/api/health")
def api_health(req):
    return _ok({"version": __version__, "uptime_s": int(time.time() - CORE.started_at)})


# ---------------------------------------------------------------------------
# Mission Control V1 — contrat spatial_mission_control.js. Tout vient de
# `CORE.missions` (observateur du bus) : aucune donnée n'est fabriquée ici.
# ---------------------------------------------------------------------------
@router.get("/api/missions")
def api_missions(req):
    return _ok(CORE.missions.snapshot())


@router.get("/api/missions/history")
def api_missions_history(req):
    q = req["query"]
    return _ok(CORE.missions.history(
        status=q.get("status", ["ALL"])[0],
        limit=int(q.get("limit", ["40"])[0]),
        offset=int(q.get("offset", ["0"])[0]),
    ))


@router.get("/api/missions/health")
def api_missions_health(req):
    from .mission_control import health_report

    return _ok(health_report(CORE))


@router.get("/api/missions/<mission_id>")
def api_missions_detail(req, mission_id):
    mission = CORE.missions.detail(mission_id)
    if not mission:
        return _err("Mission inconnue : " + mission_id, 404)
    return _ok({"mission": mission})


@router.get("/api/missions/<mission_id>/timeline")
def api_missions_timeline(req, mission_id):
    return _ok({"timeline": CORE.missions.timeline(mission_id)})


@router.get("/api/missions/<mission_id>/diagnostic")
def api_missions_diagnostic(req, mission_id):
    return _ok({"diagnostic": CORE.missions.diagnostic(mission_id)})


# ---------------------------------------------------------------------------
# Self Upgrade V1
# ---------------------------------------------------------------------------
@router.get("/api/self-upgrade/build-id")
def api_self_upgrade_build_id(req):
    return _ok({"build_id": "JARVIS_SELF_UPGRADE_V1", "version": __version__})


@router.get("/api/self-upgrade/config")
def api_self_upgrade_config(req):
    cfg = CORE.self_upgrade.config()
    cfg["supervisor"] = CORE.self_upgrade._client.status() if CORE.self_upgrade._client.ping() else {
        "ok": False, "reachable": False}
    return _ok({"config": cfg})


@router.post("/api/self-upgrade/config")
def api_self_upgrade_config_update(req):
    allowed = {"base_url", "orchestrator_model", "coder_model", "candidate_port",
               "supervisor_url", "main_port", "python", "max_attempts", "enabled"}
    values = {k: v for k, v in (req["body"] or {}).items() if k in allowed}
    if not values:
        return _err("Aucun réglage valide fourni.")
    updated = CORE.self_upgrade.update_config(values)
    return _ok({"config": updated})


@router.post("/api/self-upgrade/run")
def api_self_upgrade_run(req):
    prompt = str((req["body"] or {}).get("prompt") or "").strip()
    if not prompt:
        return _err("prompt requis.")
    mode = str((req["body"] or {}).get("mode") or "auto")
    conversation_id = str((req["body"] or {}).get("conversation_id") or "")
    result = CORE.self_upgrade.run(prompt, mode=mode, conversation_id=conversation_id)
    if not result.get("ok"):
        return _err(result.get("error", "échec"), 409)
    return _ok(result)


@router.post("/api/self-upgrade/cancel")
def api_self_upgrade_cancel(req):
    return _ok(CORE.self_upgrade.cancel())


@router.get("/api/self-upgrade/active")
def api_self_upgrade_active(req):
    active = CORE.self_upgrade.active_upgrade()
    return _ok({"active": active})


@router.get("/api/self-upgrade/upgrades")
def api_self_upgrade_list(req):
    items = CORE.self_upgrade.history.list(limit=30)
    return _ok({"upgrades": items})


@router.get("/api/self-upgrade/upgrades/<upgrade_id>")
def api_self_upgrade_detail(req, upgrade_id):
    row = CORE.self_upgrade.history.get(upgrade_id)
    if row is None:
        return _err("upgrade inconnue", 404)
    return _ok(CORE.self_upgrade._with_report(row))


@router.post("/api/self-upgrade/upgrades/<upgrade_id>/install")
def api_self_upgrade_install(req, upgrade_id):
    result = CORE.self_upgrade.install(upgrade_id)
    if not result.get("ok"):
        return _err(result.get("error", "installation refusée"), 409)
    return _ok(result)


@router.post("/api/self-upgrade/upgrades/<upgrade_id>/rollback")
def api_self_upgrade_rollback(req, upgrade_id):
    result = CORE.self_upgrade.rollback_u(upgrade_id)
    if not result.get("ok"):
        return _err(result.get("error", "rollback refusé"), 409)
    return _ok(result)


@router.post("/api/self-upgrade/upgrades/<upgrade_id>/history")
def api_self_upgrade_history_detail(req, upgrade_id):
    return _ok({"files": CORE.self_upgrade.history.files(upgrade_id)})


# ---------------------------------------------------------------------------
# Conversation / commandes
# ---------------------------------------------------------------------------
@router.post("/api/command")
def api_command(req):
    text = str(req["body"].get("text") or "").strip()
    if not text:
        return _err("Commande vide.")
    print('[CHAT-TRACE] request received conversation_id=' + str(req['body'].get('conversation_id') or '') + ' source=' + str(req['body'].get('source') or 'text') + ' attachments=' + str(len(req['body'].get('attachments') or [])), flush=True)
    print('[CHAT-TRACE] orchestrator started', flush=True)
    result = CORE.orchestrator.handle(
        text,
        conversation_id=str(req["body"].get("conversation_id") or ""),
        source=str(req["body"].get("source") or "text"),
        confirmation_id=str(req["body"].get("confirmation_id") or ""),
        background=bool(req["body"].get("background", False)),
        attachments=[str(a) for a in (req["body"].get("attachments") or [])],
    )
    print('[CHAT-TRACE] response ready conversation_id=' + str(result.get('conversation_id') or ''), flush=True)
    return _ok(result)


@router.post("/api/brainrot-sync/confirm-scope")
def api_brainrot_sync_scope(req):
    """Ouvre une portee de confirmation liee a la selection exacte affichee."""
    from .brainrot_sync import SyncService

    body = req["body"]
    plan = CORE.sync_plans.get(str(body.get("plan_hash") or ""))
    if not plan:
        return _err("Plan de synchronisation inconnu ou expire.", 409)
    selection = [str(x) for x in (body.get("selection") or [])]
    if not selection:
        return _err("Aucune entree selectionnee.", 400)
    return _ok(SyncService(CORE).open_confirmation(
        plan, selection, request_id=str(body.get("request_id") or "")))


@router.post("/api/brainrot-sync/refresh")
def api_brainrot_sync_refresh(req):
    """Relit Sheet + site et recalcule le plan. Lecture seule."""
    from .sync_audit import REFRESH_COMPLETED, REFRESH_STARTED, SyncAudit

    audit = SyncAudit(CORE)
    request_id = str(req["body"].get("request_id") or "")
    try:
        audit.emit(REFRESH_STARTED, {"request_id": request_id})
    except Exception:
        pass
    result = CORE.orchestrator.refresh_sync(
        str(req["body"].get("url") or ""), request_id=request_id,
        conversation_id=str(req["body"].get("conversation_id") or ""))
    if not result.get("ok"):
        return _ok({"ok": False, "error": result.get("error", "REFRESH_FAILED"),
                    "response": result.get("response", "")})
    payload = result.get("analysis_workspace") or {}
    try:
        audit.emit(REFRESH_COMPLETED, {
            "request_id": request_id,
            "plan_id": (payload.get("sync") or {}).get("plan_hash", "")})
    except Exception:
        pass
    return _ok({"ok": True, "comparison": payload.get("comparison"),
                "sync": payload.get("sync"), "evidence": payload.get("evidence"),
                "metrics": payload.get("metrics"), "warnings": payload.get("warnings"),
                "recommendations": payload.get("recommendations"),
                "duration_ms": payload.get("duration_ms", 0)})


@router.post("/api/brainrot-sync/apply")
def api_brainrot_sync_apply(req):
    """Applique un plan de synchronisation deja prepare, apres confirmation.

    Trois verrous : le plan doit exister cote serveur (il n'est jamais
    reconstruit depuis la requete), l'utilisateur doit avoir confirme
    explicitement, et l'empreinte du plan doit correspondre. Le modele n'a
    aucun acces a cette route : elle est appelee par l'interface.
    """
    from .brainrot_sync import SyncService

    body = req["body"]
    plan_hash = str(body.get("plan_hash") or "")
    selection = [str(x) for x in (body.get("selection") or [])]
    approved = bool(body.get("approved", False))
    plan = CORE.sync_plans.get(plan_hash)
    if not plan:
        return _err("Plan de synchronisation inconnu ou expire. Relance une preparation.", 409)
    if not approved:
        return _err("Confirmation explicite requise.", 428)
    if not selection:
        return _err("Aucune entree selectionnee.", 400)
    result = SyncService(CORE).apply(
        plan, selection,
        confirmation={"approved": True, "plan_hash": plan_hash,
                      "at": time.time(), "source": "ui"},
        request_id=str(body.get("request_id") or ""),
        # Renvoye par l'interface apres que l'utilisateur a approuve l'invite
        # d'ecriture du runner. Sans lui, l'invite est simplement remontee.
        confirmation_id=str(body.get("confirmation_id") or ""),
        scope_id=str(body.get("scope_id") or ""),
        idempotency_key=str(body.get("idempotency_key") or ""))
    if not result.get("ok"):
        return _ok({**result, "ok": False})
    return _ok(result)


@router.post("/api/brainrot-sync/rollback")
def api_brainrot_sync_rollback(req):
    """Restaure une sauvegarde produite par une application precedente."""
    from .brainrot_sync import SyncService

    backup_path = str(req["body"].get("backup_path") or "")
    if not backup_path:
        return _err("backup_path manquant.", 400)
    return _ok(SyncService(CORE).rollback(
        backup_path, sync_id=str(req["body"].get("sync_id") or ""),
        confirmation_id=str(req["body"].get("confirmation_id") or "")))


@router.post("/api/confirm")
def api_confirm(req):
    cid = str(req["body"].get("confirmation_id") or "")
    approved = bool(req["body"].get("approved", False))
    if not cid:
        return _err("confirmation_id manquant.")
    return _ok(CORE.orchestrator.resume_confirmation(cid, approved))


@router.get("/api/confirmations")
def api_confirmations(req):
    return _ok({"pending": CORE.permissions.pending_list()})


@router.get("/api/conversations")
def api_conversations(req):
    return _ok({"conversations": CORE.conversations.list(
        limit=int(req["query"].get("limit", ["50"])[0]),
        search=req["query"].get("search", [""])[0]),
        "current": CORE.conversations.current_id()})


@router.post("/api/conversations")
def api_conversation_create(req):
    return _ok({"conversation": CORE.conversations.create(str(req["body"].get("title") or ""))})


@router.get("/api/conversations/<cid>")
def api_conversation_get(req, cid):
    conv = CORE.conversations.get(cid)
    if not conv:
        return _err("Conversation introuvable.", 404)
    conv["messages"] = CORE.conversations.messages(cid, limit=int(req["query"].get("limit", ["200"])[0]))
    conv["tasks"] = CORE.tasks.list(conversation_id=cid, limit=30)
    return _ok({"conversation": conv})


@router.put("/api/conversations/<cid>")
def api_conversation_update(req, cid):
    if "title" in req["body"]:
        CORE.conversations.rename(cid, str(req["body"]["title"]))
    if req["body"].get("current"):
        CORE.conversations.set_current(cid)
    return _ok({"conversation": CORE.conversations.get(cid)})


@router.delete("/api/conversations/<cid>")
def api_conversation_delete(req, cid):
    return _ok({"deleted": CORE.conversations.delete(cid)})


# ---------------------------------------------------------------------------
# Voix — machine à états et politique de greeting
# ---------------------------------------------------------------------------
@router.post("/api/voice/session")
def api_voice_session(req):
    """Ouvre ou reprend une session. Une reconnexion reprend la session : pas de greeting."""
    session = CORE.sessions.open(str(req["body"].get("client_id") or ""))
    return _ok({"session": session, "voice": CORE.settings.section("voice"),
                "state": CORE.voice.snapshot()})


@router.post("/api/voice/greeting")
def api_voice_greeting(req):
    """Autorité unique du greeting. Renvoie `speak: false` dans tous les autres cas."""
    session_id = str(req["body"].get("session_id") or "")
    if not session_id:
        return _err("session_id manquant.")
    should, text = CORE.sessions.should_greet(session_id)
    return _ok({"speak": should, "text": text})


@router.post("/api/voice/state")
def api_voice_state(req):
    target = str(req["body"].get("state") or "")
    ok, state = CORE.voice.transition(target, reason=str(req["body"].get("reason") or ""),
                                      utterance=str(req["body"].get("utterance") or ""))
    return _ok({"accepted": ok, "state": state, "snapshot": CORE.voice.snapshot()})


@router.post("/api/voice/heartbeat")
def api_voice_heartbeat(req):
    """Heartbeat volontairement inerte vis-à-vis du greeting."""
    session_id = str(req["body"].get("session_id") or "")
    if session_id:
        CORE.sessions.touch(session_id)
    return _ok({"state": CORE.voice.snapshot()})


@router.post("/api/voice/transcript")
def api_voice_transcript(req):
    CORE.events.emit("voice.transcript", {"text": str(req["body"].get("text") or "")[:500],
                                          "final": bool(req["body"].get("final", True))})
    return _ok()


# ---------------------------------------------------------------------------
# Synthèse vocale locale (Piper)
# ---------------------------------------------------------------------------
@router.get("/api/tts/voices")
def api_tts_voices(req):
    """Voix françaises Piper : état local + moteur."""
    voices = CORE.tts.voices()
    return _ok({
        "engine": CORE.tts.engine_status(),
        "voices": voices,
        "selected": str(CORE.settings.get("voice", "voice", "") or "") or CORE.tts.default_voice(),
        "provider": CORE.settings.get("voice", "tts_provider", "browser"),
        "fallback": CORE.tts_fallback.engine_status(),
        "fallback_voices": CORE.tts_fallback.voices(),
    })


@router.get("/api/tts/status")
def api_tts_status(req):
    return _ok({
        "engine": CORE.tts.engine_status(),
        "summary": CORE.tts.summary(),
        "fallback": CORE.tts_fallback.engine_status(),
        "fallback_summary": CORE.tts_fallback.summary(),
    })


@router.post("/api/tts/synthesize")
def api_tts_synthesize(req):
    text = str(req["body"].get("text") or "").strip()
    if not text:
        return _err("Texte vide.")
    settings = CORE.settings
    voice = str(req["body"].get("voice") or settings.get("voice", "voice", ""))
    rate = float(req["body"].get("rate") or settings.get("voice", "speech_rate", 1.0))
    # Locuteur : une voix multi-locuteurs n'est adressable que par lui.
    speaker = str(req["body"].get("speaker")
                  if req["body"].get("speaker") is not None
                  else settings.get("voice", "speaker", ""))
    expressivity = req["body"].get("expressivity")
    if expressivity is None:
        expressivity = settings.get("voice", "expressivity", 0.0)
    if req["body"].get("sanitize", True):
        from .speech_sanitizer import sanitize_for_speech
        spoken = sanitize_for_speech(text)
        if not spoken:
            # Le message n'était que des emojis / du décor : il n'y a rien à
            # prononcer. Retomber sur le texte brut ferait lire « fusée ».
            return _err("Rien à prononcer dans ce message.", 204)
        text = spoken
    if len(text) > 4000:
        text = text[:4000]
    wav = CORE.tts.synthesize(text, voice_id=voice, rate=rate, speaker=speaker,
                              expressivity=float(expressivity or 0.0))
    if wav is not None:
        return RawResponse(wav, "audio/wav")
    # Piper n'a rien à dire (aucune voix installée, ou modèle illisible) : plutôt
    # que de rester muet, on bascule sur les voix Edge. Le repli sort le texte de
    # la machine, il est donc signalé tel quel dans /api/tts/status.
    fallback = CORE.tts_fallback.synthesize(text, voice_id=voice, rate=rate)
    if fallback is not None:
        body, mime = fallback
        return RawResponse(body, mime)
    return _err("Voix Piper indisponible : installe une voix française puis réessaie.", 503)


@router.post("/api/tts/install")
def api_tts_install(req):
    voice_id = str(req["body"].get("voice") or "").strip()
    try:
        if voice_id:
            result = CORE.tts.install(voice_id)
        else:
            result = CORE.tts.install_all_french()
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"Téléchargement impossible : {exc}", 502)
    return _ok({"installed": result})


# ---------------------------------------------------------------------------
# Diagnostic d'installation
# ---------------------------------------------------------------------------
@router.get("/api/doctor")
def api_doctor(req):
    """Etat reel de chaque brique externe (Blender, ComfyUI, Piper, GPU...)."""
    from .doctor import diagnose

    only = [n for n in (req["query"].get("check") or []) if n]
    return _ok(diagnose(CORE, only or None))


@router.post("/api/doctor/repair")
def api_doctor_repair(req):
    """Installe les dependances manquantes qui ne demandent aucun choix humain."""
    from .doctor import repair

    names = req["body"].get("checks")
    if names is not None and not isinstance(names, list):
        return _err("`checks` doit etre une liste de noms de controles.")
    try:
        return _ok(repair(CORE, [str(n) for n in names] if names else None))
    except Exception as exc:
        return _err(f"Reparation impossible : {exc}", 500)


# ---------------------------------------------------------------------------
# Reconnaissance vocale locale (repli serveur quand le navigateur ne peut pas)
# ---------------------------------------------------------------------------
@router.get("/api/stt/status")
def api_stt_status(req):
    return _ok(CORE.stt.status())


@router.post("/api/stt/transcribe")
def api_stt_transcribe(req):
    """Transcrit un enregistrement envoyé en base64 (champ `audio`)."""
    import base64
    import binascii

    payload = str(req["body"].get("audio") or "")
    if "," in payload[:64] and payload.lstrip().startswith("data:"):
        payload = payload.split(",", 1)[1]  # data URL renvoyée par MediaRecorder
    if not payload.strip():
        return _err("Aucun audio reçu.")
    try:
        audio = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return _err("Audio illisible : base64 invalide.")
    suffix = str(req["body"].get("format") or "webm").strip().lstrip(".").lower()
    if suffix not in {"webm", "ogg", "wav", "mp3", "m4a", "flac"}:
        suffix = "webm"
    language = str(req["body"].get("language") or "").strip()
    try:
        result = CORE.stt.transcribe(audio, language=language, suffix="." + suffix)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        return _err(f"Transcription impossible : {exc}", 500)
    return _ok(result)


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------
@router.get("/api/tasks")
def api_tasks(req):
    return _ok({"tasks": CORE.tasks.list(status=req["query"].get("status", [""])[0],
                                         limit=int(req["query"].get("limit", ["50"])[0])),
                "stats": CORE.tasks.stats()})


@router.get("/api/tasks/<tid>")
def api_task_detail(req, tid):
    task = CORE.tasks.detail(tid)
    return _ok({"task": task}) if task else _err("Tâche introuvable.", 404)


@router.post("/api/tasks")
def api_task_create(req):
    name = str(req["body"].get("name") or "").strip()
    if not name:
        return _err("Nom manquant.")
    return _ok({"task": CORE.tasks.create(name=name, kind="manual", agent="jarvis",
                                          meta={"note": str(req["body"].get("note") or "")})})


@router.post("/api/tasks/<tid>/cancel")
def api_task_cancel(req, tid):
    return _ok({"cancelled": CORE.tasks.cancel(tid)})


@router.post("/api/tasks/<tid>/complete")
def api_task_complete(req, tid):
    return _ok({"completed": CORE.tasks.complete(tid, str(req["body"].get("result") or "Terminé."))})


# ---------------------------------------------------------------------------
# Agents / outils
# ---------------------------------------------------------------------------
@router.get("/api/agents")
def api_agents(req):
    return _ok({"agents": CORE.agents.list()})


@router.post("/api/agents/<aid>/run")
def api_agent_run(req, aid):
    instruction = str(req["body"].get("instruction") or "").strip()
    if not instruction:
        return _err("Instruction manquante.")
    return _ok(CORE.orchestrator.run_agent(aid, instruction))


@router.post("/api/agents/<aid>/toggle")
def api_agent_toggle(req, aid):
    return _ok({"updated": CORE.agents.set_enabled(aid, bool(req["body"].get("enabled", True)))})


@router.get("/api/tools")
def api_tools(req):
    return _ok({"tools": [t.public(CORE) for t in registry.all()],
                "categories": registry.categories()})


@router.post("/api/tools/<tool_id>/toggle")
def api_tool_toggle(req, tool_id):
    return _ok({"updated": registry.set_enabled(tool_id, bool(req["body"].get("enabled", True)))})


@router.post("/api/tools/<tool_id>/run")
def api_tool_run(req, tool_id):
    from .tools.runner import ConfirmationRequired

    try:
        result = CORE.runner.run(tool_id, req["body"].get("arguments") or {}, agent="jarvis",
                                 confirmed=bool(req["body"].get("confirmed", False)))
    except ConfirmationRequired as exc:
        return _ok({"needs_confirmation": {"id": exc.pending.id, "action": exc.pending.action,
                                           "risk": exc.pending.risk, "reason": exc.pending.reason,
                                           "speech": getattr(exc.pending, "speech", "")},
                    "message": exc.message})
    return _ok({"result": result.to_dict()})


# ---------------------------------------------------------------------------
# Connecteurs
# ---------------------------------------------------------------------------
@router.get("/api/connectors")
def api_connectors(req):
    return _ok({"connectors": CORE.connectors.list(), "types": type_catalog()})


@router.post("/api/connectors")
def api_connector_create(req):
    try:
        return _ok({"connector": CORE.connectors.create(req["body"])})
    except ValueError as exc:
        return _err(str(exc))


@router.get("/api/connectors/<cid>")
def api_connector_get(req, cid):
    c = CORE.connectors.get(cid)
    return _ok({"connector": c}) if c else _err("Connecteur introuvable.", 404)


@router.put("/api/connectors/<cid>")
def api_connector_update(req, cid):
    try:
        return _ok({"connector": CORE.connectors.update(cid, req["body"])})
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/api/connectors/<cid>")
def api_connector_delete(req, cid):
    return _ok({"deleted": CORE.connectors.delete(cid)})


@router.post("/api/connectors/<cid>/test")
def api_connector_test(req, cid):
    ok, detail = CORE.connectors.test(cid)
    CORE.llm.invalidate()
    return _ok({"connected": ok, "detail": detail, "connector": CORE.connectors.get(cid)})


@router.post("/api/connectors/<cid>/toggle")
def api_connector_toggle(req, cid):
    return _ok({"connector": CORE.connectors.set_enabled(
        cid, bool(req["body"].get("enabled", True)), CORE.llm)})


# ---------------------------------------------------------------------------
# Mémoire / connaissances
# ---------------------------------------------------------------------------
@router.get("/api/memory")
def api_memory(req):
    q = req["query"]
    search = q.get("search", [""])[0]
    scope = q.get("scope", [""])[0]
    limit = int(q.get("limit", ["100"])[0])
    items = CORE.memory.search(search, limit=limit, scope=scope) if search \
        else CORE.memory.list(scope=scope, limit=limit)
    return _ok({"memories": items, "stats": CORE.memory.stats()})


@router.post("/api/memory")
def api_memory_add(req):
    body = req["body"]
    content = str(body.get("content") or "").strip()
    if not content:
        return _err("Contenu manquant.")
    return _ok({"memory": CORE.memory.add(
        content=content, scope=str(body.get("scope") or "user"),
        importance=int(body.get("importance") or 3), tags=body.get("tags") or [],
        source="ui", project=str(body.get("project") or ""), pinned=bool(body.get("pinned", False)))})


@router.put("/api/memory/<mid>")
def api_memory_update(req, mid):
    item = CORE.memory.update(mid, **req["body"])
    return _ok({"memory": item}) if item else _err("Souvenir introuvable.", 404)


@router.delete("/api/memory/<mid>")
def api_memory_delete(req, mid):
    return _ok({"deleted": CORE.memory.delete(mid)})


@router.get("/api/memory/graph")
def api_memory_graph(req):
    return _ok({"graph": CORE.memory.graph()})


@router.get("/api/knowledge")
def api_knowledge(req):
    search = req["query"].get("search", [""])[0]
    items = CORE.memory.knowledge_search(search) if search else CORE.memory.knowledge_list()
    return _ok({"items": items})


@router.post("/api/knowledge")
def api_knowledge_add(req):
    body = req["body"]
    if not body.get("title") or not body.get("content"):
        return _err("Titre et contenu requis.")
    return _ok({"item": CORE.memory.knowledge_add(
        title=str(body["title"]), content=str(body["content"]),
        kind=str(body.get("kind") or "note"), tags=body.get("tags") or [], source="ui",
        project=str(body.get("project") or ""))})


@router.put("/api/knowledge/<kid>")
def api_knowledge_update(req, kid):
    item = CORE.memory.knowledge_update(kid, **req["body"])
    return _ok({"item": item}) if item else _err("Fiche introuvable.", 404)


@router.delete("/api/knowledge/<kid>")
def api_knowledge_delete(req, kid):
    return _ok({"deleted": CORE.memory.knowledge_delete(kid)})


@router.get("/api/learning")
def api_learning(req):
    return _ok({"learning": CORE.idle_learning.status()})


@router.post("/api/learning/pause")
def api_learning_pause(req):
    CORE.idle_learning._manual_paused = True
    CORE.idle_learning.state = "PAUSED"
    CORE.idle_learning.touch()
    CORE.idle_learning.state = "PAUSED"
    return _ok({"learning": CORE.idle_learning.status()})


@router.post("/api/learning/resume")
def api_learning_resume(req):
    CORE.idle_learning._manual_paused = False
    CORE.idle_learning.last_activity_at = time.time() - int(CORE.settings.get("learning", "idle_after_s", 600))
    CORE.idle_learning.state = "IDLE"
    return _ok({"learning": CORE.idle_learning.status()})


@router.post("/api/learning/run")
def api_learning_run(req):
    return _ok({"result": CORE.idle_learning.run_manual(), "learning": CORE.idle_learning.status()})


@router.post("/api/learning/migrate")
def api_learning_migrate(req):
    return _ok({"migration": CORE.idle_learning.migrate(), "learning": CORE.idle_learning.status()})


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------
@router.get("/api/calendar")
def api_calendar(req):
    days = int(req["query"].get("days", ["14"])[0])
    return _ok({"events": CORE.calendar.upcoming(days=days), "today": CORE.calendar.today()})


@router.post("/api/calendar")
def api_calendar_add(req):
    body = req["body"]
    title = str(body.get("title") or "").strip()
    when = body.get("start")
    if not title or not when:
        return _err("Titre et date requis.")
    ts = float(when) if isinstance(when, (int, float)) else CORE.calendar.parse_when(str(when))
    if not ts:
        return _err("Date non comprise.")
    return _ok({"event": CORE.calendar.add(title=title, start_at=ts,
                                           duration_min=int(body.get("duration_min") or 60),
                                           description=str(body.get("description") or ""))})


@router.delete("/api/calendar/<eid>")
def api_calendar_delete(req, eid):
    return _ok({"deleted": CORE.calendar.delete(eid)})


# ---------------------------------------------------------------------------
# Workflows / automatisations
# ---------------------------------------------------------------------------
@router.get("/api/workflows")
def api_workflows(req):
    return _ok({"workflows": CORE.automations.list(), "runs": CORE.automations.runs(limit=20)})


@router.post("/api/workflows")
def api_workflow_create(req):
    body = req["body"]
    name = str(body.get("name") or "").strip()
    instruction = str(body.get("instruction") or "").strip()
    trigger = body.get("trigger")
    if isinstance(trigger, str):
        trigger = CORE.automations.parse_trigger(trigger)
    if not name or not trigger:
        return _err("Nom et déclencheur requis.")
    return _ok({"workflow": CORE.automations.create(
        name=name, instruction=instruction or name, trigger=trigger,
        description=str(body.get("description") or ""), steps=body.get("steps"))})


@router.put("/api/workflows/<wid>")
def api_workflow_update(req, wid):
    wf = CORE.automations.update(wid, req["body"])
    return _ok({"workflow": wf}) if wf else _err("Automatisation introuvable.", 404)


@router.delete("/api/workflows/<wid>")
def api_workflow_delete(req, wid):
    return _ok({"deleted": CORE.automations.delete(wid)})


@router.post("/api/workflows/<wid>/run")
def api_workflow_run(req, wid):
    return _ok(CORE.automations.run(wid, reason="manuel"))


@router.post("/api/workflows/<wid>/toggle")
def api_workflow_toggle(req, wid):
    return _ok({"updated": CORE.automations.set_enabled(wid, bool(req["body"].get("enabled", True)))})


@router.post("/api/hooks/<token>")
def api_hook(req, token):
    return _ok(CORE.automations.trigger_webhook(token, req["body"]))


# ---------------------------------------------------------------------------
# Tâches Discord planifiées
# ---------------------------------------------------------------------------
@router.get("/api/discord/schedules")
def api_discord_schedules(req):
    return _ok({"schedules": CORE.discord_scheduler.list(),
                "runs": CORE.discord_scheduler.runs(limit=30)})


@router.post("/api/discord/schedules")
def api_discord_schedule_create(req):
    body = req["body"]
    try:
        task = CORE.discord_scheduler.add(
            name=str(body.get("name") or ""),
            interval=body.get("interval"),
            tool_to_call=str(body.get("tool_to_call") or ""),
            target_channel_id=str(body.get("target_channel_id") or ""),
            params=body.get("params") or {},
            status=str(body.get("status") or "ACTIVE"),
            allow_destructive=bool(body.get("allow_destructive")),
            source="api",
        )
    except ScheduleError as exc:
        return _err(str(exc))
    return _ok({"schedule": task})


@router.put("/api/discord/schedules/<sid>")
def api_discord_schedule_update(req, sid):
    try:
        task = CORE.discord_scheduler.update(sid, req["body"])
    except ScheduleError as exc:
        return _err(str(exc))
    return _ok({"schedule": task}) if task else _err("Tâche planifiée introuvable.", 404)


@router.delete("/api/discord/schedules/<sid>")
def api_discord_schedule_delete(req, sid):
    return _ok({"deleted": CORE.discord_scheduler.delete(sid)})


@router.post("/api/discord/schedules/<sid>/status")
def api_discord_schedule_status(req, sid):
    """Bascule ACTIVE / PAUSED sans perdre l'historique de la tâche."""
    try:
        task = CORE.discord_scheduler.set_status(sid, str(req["body"].get("status") or ""))
    except ScheduleError as exc:
        return _err(str(exc))
    return _ok({"schedule": task}) if task else _err("Tâche planifiée introuvable.", 404)


@router.post("/api/discord/schedules/<sid>/run")
def api_discord_schedule_run(req, sid):
    return _ok(CORE.discord_scheduler.run_now(sid, reason="manuel"))


# ---------------------------------------------------------------------------
# Feed / audit / réglages
# ---------------------------------------------------------------------------
@router.get("/api/feed")
def api_feed(req):
    return _ok({"items": CORE.events.feed_items(limit=int(req["query"].get("limit", ["40"])[0]))})


@router.post("/api/feed/read")
def api_feed_read(req):
    CORE.events.mark_feed_read(req["body"].get("id"))
    return _ok()


@router.get("/api/audit")
def api_audit(req):
    q = req["query"]
    return _ok({"entries": CORE.audit.entries(limit=int(q.get("limit", ["100"])[0]),
                                              offset=int(q.get("offset", ["0"])[0]),
                                              search=q.get("search", [""])[0]),
                "total": CORE.audit.count()})


@router.get("/api/settings")
def api_settings(req):
    return _ok({"settings": CORE.settings.all(), "models": CORE.llm.model_options(),
                "locked": {"voice.greeting_frequency": "once_per_session"}})


@router.put("/api/settings/<section>")
def api_settings_update(req, section):
    try:
        updated = CORE.settings.update(section, req["body"])
    except ValueError as exc:
        return _err(str(exc))
    if section in {"ai", "memory"}:
        CORE.llm.invalidate()
    CORE.events.emit("settings.updated", {"section": section})
    CORE.audit.record(action=f"Réglages modifiés: {section}", tool="settings")
    return _ok({"section": section, "values": updated})


@router.get("/api/discord/status")
def api_discord_status(req):
    """Statut Discord et permissions du salon Blog, sans aucun envoi."""
    from .discord_engine import DiscordEngine
    engine = getattr(CORE, "discord", None)
    if engine is None:
        engine = DiscordEngine(CORE)
        CORE.discord = engine
    status = engine.status()
    channel_id = str(CORE.settings.get("blog", "discord_channel_id", "") or "")
    if engine.connected and channel_id.isdigit():
        try:
            status["blog_channel"] = engine.submit(engine.verify_channel(channel_id))
        except Exception as exc:
            status["blog_channel"] = {"ok": False, "error": engine.redact(exc)}
    else:
        status["blog_channel"] = {"ok": False, "channel_id": channel_id,
                                   "reason": "Bot non connecté ou salon non configuré."}
    return _ok(status)


@router.post("/api/discord/connect")
def api_discord_connect(req):
    """Connexion puis contrôle lecture seule du salon configuré."""
    from .discord_engine import DiscordEngine
    engine = getattr(CORE, "discord", None)
    if engine is None:
        engine = DiscordEngine(CORE)
        CORE.discord = engine
    result = engine.start()
    if result.get("ok"):
        channel_id = str(CORE.settings.get("blog", "discord_channel_id", "") or "")
        if channel_id.isdigit():
            try:
                result["blog_channel"] = engine.submit(engine.verify_channel(channel_id))
            except Exception as exc:
                result["blog_channel"] = {"ok": False, "error": engine.redact(exc)}
    return _ok(result)


@router.get("/api/blog/dashboard")
def api_blog_dashboard(req):
    from .blog_editorial import EditorialAgent
    agent = getattr(CORE, "blog_editorial", None) or EditorialAgent(CORE)
    CORE.blog_editorial = agent
    limit = int(req["query"].get("limit", ["50"])[0])
    posts = agent.site.posts(limit=max(1, min(limit, 200)))
    events = agent.publisher.events(limit=100)
    published = {"published", "publish", "public", "online", "active"}
    buckets = {"drafts": [], "review": [], "published": [], "failures": []}
    for post in posts:
        status = str(post.get("status") or "").lower()
        item = {"id": post.get("id"), "title": post.get("title"), "slug": post.get("slug"),
                "status": post.get("status"), "content": post.get("content", ""),
                "excerpt": post.get("excerpt", ""), "url": f"https://brainrot-fortnite.com/blog/{post.get('slug')}"}
        if status == "draft": buckets["drafts"].append(item)
        elif status in published: buckets["published"].append(item)
        else: buckets["review"].append(item)
    buckets["failures"] = [e for e in events if e.get("status") == "FAILED"]
    return _ok({"buckets": buckets, "settings": agent.auto_settings(),
                "scheduler": CORE.editorial_scheduler.status()})


@router.post("/api/blog/auto/run")
def api_blog_auto_run(req):
    return _ok(CORE.editorial_scheduler.run_now(reason="manuel"))


@router.get("/api/llm")
def api_llm(req):
    return _ok({"providers": CORE.llm.status(max_age=0, blocking=True),
                "models": CORE.llm.model_options()})


@router.post("/api/system/backup")
def api_backup(req):
    path = CORE.db.backup("manuel")
    return _ok({"backup": str(path)})


@router.post("/api/system/focus")
def api_focus(req):
    CORE.focus_ui(req["server_port"])
    return _ok()


# ---------------------------------------------------------------------------
# Génération d'images
# ---------------------------------------------------------------------------
from .imagegen import IMAGE_DIR  # noqa: E402  (routes below need it)


@router.get("/api/images")
def api_images(req):
    conversation_id = req["query"].get("conversation_id", [""])[0]
    limit = int(req["query"].get("limit", ["50"])[0])
    return _ok({"jobs": CORE.imagegen.list(conversation_id, limit=limit),
                **CORE.imagegen.status()})


@router.get("/api/images/history")
def api_image_history(req):
    limit = int(req["query"].get("limit", ["50"])[0])
    return _ok({"history": CORE.imagegen.history.list(limit)})


@router.get("/api/images/backends")
def api_image_backends(req):
    return _ok(CORE.imagegen.status())


@router.get("/api/images/analyze")
def api_image_analyze(req):
    prompt = str(req["query"].get("prompt", [""])[0] or "").strip()
    if not prompt:
        return _err("Prompt manquant.")
    return _ok({"intent": CORE.imagegen.analyze(prompt)})


@router.get("/api/images/<job_id>")
def api_image_job(req, job_id):
    job = CORE.imagegen.get(job_id)
    if not job:
        return _err("Job image inconnu.", 404)
    return _ok({"job": job})


@router.post("/api/images/<job_id>/cancel")
def api_image_cancel(req, job_id):
    job = CORE.imagegen.cancel(job_id)
    if not job:
        return _err("Job image inconnu.", 404)
    return _ok({"job": job})


@router.get("/api/images/<job_id>/file")
def api_image_file(req, job_id):
    found = CORE.imagegen.file(job_id)
    if not found:
        return _err("Image introuvable.", 404)
    data, ctype = found
    return RawResponse(data, ctype)


# ---------------------------------------------------------------------------
# Documents generes (factures / devis)
# ---------------------------------------------------------------------------
@router.get("/api/documents")
def api_documents(req):
    """Liste les documents deja produits, du plus recent au plus ancien."""
    from .invoicing import EXPORT_DIR

    items = []
    try:
        for path in sorted(EXPORT_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
            kind, _, number = path.stem.partition("_")
            items.append({"filename": path.name, "number": number, "kind": kind,
                          "bytes": path.stat().st_size, "modified_at": path.stat().st_mtime})
    except Exception:
        items = []
    return _ok({"documents": items[:50]})


@router.get("/api/documents/<filename>")
def api_document_file(req, filename):
    """Sert un document genere.

    `filename` est compare au contenu REEL du dossier d'export : aucun chemin
    fourni par le client n'est concatene, donc aucune traversee possible.
    """
    from .invoicing import EXPORT_DIR

    wanted = str(filename or "")
    try:
        match = next((p for p in EXPORT_DIR.iterdir()
                      if p.is_file() and p.name == wanted), None)
    except Exception:
        match = None
    if match is None:
        return _err("Document introuvable.", 404)
    ctype = {".pdf": "application/pdf", ".html": "text/html; charset=utf-8"}.get(
        match.suffix.lower(), "application/octet-stream")
    return RawResponse(match.read_bytes(), ctype)


@router.post("/api/images/import")
def api_image_import(req):
    """Register an uploaded image as an image job so it can be edited.

    Image Edit V3 operates on jobs, so an imported picture becomes a job with
    ``pipeline_version = "imported"`` rather than a separate code path: the
    mask, cutout, restyle and upscale routes then work on it unchanged.
    """
    import base64

    from .imagegen import IMAGE_DIR, new_id

    body = req["body"]
    filename = str(body.get("filename") or "").strip()
    data_b64 = str(body.get("data_b64") or "").strip()
    if not data_b64:
        return _err("data_b64 requis.")
    ext = Path(filename).suffix.lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return _err("Format d'image non supporté : " + ext)
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return _err("Données base64 invalides.")
    if not raw:
        return _err("Image vide.")
    if len(raw) > 25_000_000:
        return _err("Image trop volumineuse (max 25 Mo).")

    job_id = new_id("img")
    folder = IMAGE_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "image.png"
    path.write_bytes(raw)

    job = {
        "id": job_id, "conversation_id": str(body.get("conversation_id") or ""),
        "mode": "imported", "prompt": Path(filename).name or "image importée",
        "status": "completed", "stage": "COMPLETE", "progress": 1.0,
        "file_path": str(path), "error": "", "created_at": time.time(),
        "width": 0, "height": 0, "steps": 0, "seed": 0,
        "meta": {"pipeline_version": "imported", "original_prompt": filename,
                 "source": "upload", "bytes": len(raw)},
    }
    try:
        width, height = _png_dimensions(raw)
        job["width"], job["height"] = width, height
    except Exception:
        pass
    CORE.imagegen._save(job)
    return _ok({"job": job, "url": f"/api/images/{job_id}/file"})


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        return image.size


@router.post("/api/images/<job_id>/edit")
def api_image_edit(req, job_id):
    """Run an Image Edit V3 operation on an existing job.

    Segmentation is automatic where the operation needs a selection; the route
    refuses, with the reason, when the request names a region BiRefNet cannot
    isolate, instead of editing the wrong area.
    """
    from .image_edit_graph import plan_edit
    from .image_runtime import ComfyClient, ImageRenderer
    from .imagegen import IMAGE_DIR, new_id
    from .zimage_graph import GraphBuildError

    source_job = CORE.imagegen.get(job_id) or {}
    source = str(source_job.get("file_path") or "")
    if not source:
        return _err("Image source introuvable.", 404)

    body = req["body"]
    operation = str(body.get("operation") or "").strip().casefold()
    prompt = str(body.get("prompt") or "").strip()

    target_id = new_id("img")
    job = {
        "id": target_id, "conversation_id": str(body.get("conversation_id") or ""),
        "mode": "edit", "prompt": prompt or operation,
        "status": "running", "stage": "PREPARING", "progress": 0.0,
        "file_path": "", "source_job_id": job_id, "error": "",
        "created_at": time.time(), "width": 0, "height": 0, "steps": 0, "seed": 0,
        "meta": {"pipeline_version": "v3", "edit_operation": operation,
                 "original_prompt": prompt, "source_job_id": job_id},
    }
    CORE.imagegen._save(job)
    CORE.imagegen._emit("image.generation.started", job)

    def on_progress(stage, progress, extra):
        job["stage"], job["progress"] = stage, round(progress, 3)
        CORE.imagegen._set(job, stage=stage, progress=progress)
        CORE.imagegen._emit("image.generation.progress", job,
                            stage=stage, progress=progress, **extra)

    try:
        client = ComfyClient(_comfy_base_url())
        client.health()
        uploaded = client.upload_image(source)
        renderer = ImageRenderer(client.base, on_progress=on_progress)

        if operation == "cutout":
            from .image_segmentation import build_cutout_graph
            result = renderer._execute(build_cutout_graph(uploaded), budget_s=240,
                                       expected_stages=["SEGMENTING"])
            meta = {"operation": "cutout", "segmentation": "birefnet",
                    "diffusion": False}
        else:
            # The source size lets a delivery upscale bound itself instead of
            # returning ESRGAN's raw x4 (a 1280x1792 source became 5120x7168).
            try:
                source_size = _png_dimensions(Path(source).read_bytes())
            except Exception:
                source_size = (0, 0)
            plan = plan_edit(operation, source_image=uploaded, prompt=prompt,
                             seed=int(body.get("seed") or 0),
                             mask_image=str(body.get("mask_image") or ""),
                             pad=tuple(body.get("pad") or (0, 0, 0, 0)),
                             upscale_to=tuple(body.get("upscale_to") or (0, 0)),
                             source_size=source_size)
            result = renderer.edit(plan, filename_prefix=f"jarvis_edit_{target_id}")
            meta = dict(result.metadata)
            meta["segmentation"] = "birefnet" if plan.auto_mask else ""
            meta["auto_mask"] = plan.auto_mask.as_dict() if plan.auto_mask else None
            job["seed"] = plan.seed
    except GraphBuildError as exc:
        job["error"] = str(exc)
        CORE.imagegen._set(job, status="failed", stage="failed", emit=False)
        CORE.imagegen._emit("image.generation.failed", job)
        return _err(str(exc), 422)
    except Exception as exc:
        job["error"] = str(exc)[:400]
        CORE.imagegen._set(job, status="failed", stage="failed", emit=False)
        CORE.imagegen._emit("image.generation.failed", job)
        return _err(f"Édition impossible : {exc}", 502)

    folder = IMAGE_DIR / target_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "image.png"
    path.write_bytes(result.images[-1])
    try:
        job["width"], job["height"] = _png_dimensions(result.images[-1])
    except Exception:
        pass
    job["file_path"] = str(path)
    job["meta"].update({"model": meta, "duration_s": result.seconds,
                        "vram_peak_mb": result.vram_peak_mb,
                        "stages": result.stages_seen, "bytes": len(result.images[-1])})
    CORE.imagegen._set(job, status="completed", stage="COMPLETE", progress=1.0,
                       emit=False)
    (folder / "image.json").write_text(
        json.dumps({**job, "url": ""}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    try:
        CORE.imagegen._save_history(job)
    except Exception:
        pass
    CORE.imagegen._emit("image.generation.completed", job, bytes=len(result.images[-1]))
    return _ok({"job": job, "url": f"/api/images/{target_id}/file"})


@router.post("/api/images/<job_id>/mask")
def api_image_mask(req, job_id):
    """Preview the automatic selection for an edit, before committing to it.

    Seeing the mask first is what stops a wrong selection from costing a full
    render and looking like a quality problem.
    """
    from .image_runtime import ComfyClient, ImageRenderer
    from .image_segmentation import (
        SegmentationUnavailable, build_preview_graph, plan_mask,
    )

    job = CORE.imagegen.get(job_id) or {}
    source = str(job.get("file_path") or "")
    if not source:
        return _err("Image source introuvable.", 404)
    body = req["body"]
    try:
        plan = plan_mask(
            str(body.get("operation") or "replace_background"),
            str(body.get("prompt") or ""),
            target=str(body.get("target") or ""),
            grow=body.get("grow"), feather=body.get("feather"))
    except SegmentationUnavailable as exc:
        return _err(str(exc), 422)
    try:
        client = ComfyClient(_comfy_base_url())
        client.health()
        uploaded = client.upload_image(source)
        graph = build_preview_graph(plan, uploaded)
        result = ImageRenderer(client.base)._execute(
            graph, budget_s=180, expected_stages=["GENERATING"])
    except Exception as exc:
        return _err(f"Segmentation impossible : {exc}", 502)

    out = IMAGE_DIR / job_id / "mask.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.images[-1])
    return _ok({"plan": plan.as_dict(), "seconds": result.seconds,
                "url": f"/api/images/{job_id}/mask/file"})


@router.get("/api/images/<job_id>/mask/file")
def api_image_mask_file(req, job_id):
    path = IMAGE_DIR / job_id / "mask.png"
    if not path.is_file():
        return _err("Masque introuvable.", 404)
    return RawResponse(path.read_bytes(), "image/png")


@router.post("/api/images/<job_id>/cutout")
def api_image_cutout(req, job_id):
    """Transparent PNG asset. No diffusion, so the subject cannot be altered."""
    from .image_runtime import ComfyClient, ImageRenderer
    from .image_segmentation import build_cutout_graph

    job = CORE.imagegen.get(job_id) or {}
    source = str(job.get("file_path") or "")
    if not source:
        return _err("Image source introuvable.", 404)
    try:
        client = ComfyClient(_comfy_base_url())
        client.health()
        uploaded = client.upload_image(source)
        result = ImageRenderer(client.base)._execute(
            build_cutout_graph(uploaded), budget_s=180,
            expected_stages=["GENERATING"])
    except Exception as exc:
        return _err(f"Détourage impossible : {exc}", 502)
    out = IMAGE_DIR / job_id / "cutout.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.images[-1])
    return _ok({"seconds": result.seconds,
                "url": f"/api/images/{job_id}/cutout/file"})


@router.get("/api/images/<job_id>/cutout/file")
def api_image_cutout_file(req, job_id):
    path = IMAGE_DIR / job_id / "cutout.png"
    if not path.is_file():
        return _err("Détourage introuvable.", 404)
    return RawResponse(path.read_bytes(), "image/png")


@router.post("/api/images/<job_id>/poster-text")
def api_image_poster_text(req, job_id):
    """Composite typography onto a generated poster (Pillow, not diffusion)."""
    from .poster_text import PosterTextError, compose_poster_text, layout_from_lines

    job = CORE.imagegen.get(job_id) or {}
    source = str(job.get("file_path") or "")
    if not source:
        return _err("Image source introuvable.", 404)
    lines = req["body"].get("lines")
    if not isinstance(lines, list) or not any(str(x).strip() for x in lines):
        return _err("Aucune ligne de texte fournie.")
    try:
        out = compose_poster_text(
            source, layout_from_lines(lines, scrim=float(req["body"].get("scrim") or 0)),
            IMAGE_DIR / job_id / "poster.png")
    except PosterTextError as exc:
        return _err(str(exc), 422)
    return _ok({"url": f"/api/images/{job_id}/poster/file", "path": str(out)})


@router.get("/api/images/<job_id>/poster/file")
def api_image_poster_file(req, job_id):
    path = IMAGE_DIR / job_id / "poster.png"
    if not path.is_file():
        return _err("Affiche composée introuvable.", 404)
    return RawResponse(path.read_bytes(), "image/png")


def _comfy_base_url() -> str:
    for backend in CORE.imagegen.backends():
        if backend.get("kind") == "comfyui":
            return str(backend.get("base_url") or "http://127.0.0.1:8188")
    return "http://127.0.0.1:8188"


@router.post("/api/images/generate")
def api_image_generate(req):
    """Génération directe (bouton UI, tests) — même chaîne que l'outil du LLM."""
    from .imagegen import ImageBackendUnavailable

    body = req["body"]
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return _err("Prompt manquant.")
    try:
        # width/height default to 0, not 1024: the V2 pipeline picks a
        # resolution from the image type (a poster is not square), and an
        # unrequested 1024 would silently override it.
        source_job_id = str(body.get("source_job_id") or "")
        source_path = str(body.get("source_path") or "")
        if source_job_id and not source_path:
            source = CORE.imagegen.get(source_job_id) or {}
            source_path = str(source.get("file_path") or "")
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        job = CORE.imagegen.generate(
            prompt, conversation_id=str(body.get("conversation_id") or ""),
            mode=str(body.get("mode") or "generate"),
            negative_prompt=str(body.get("negative_prompt") or ""),
            width=int(body.get("width") or 0), height=int(body.get("height") or 0),
            steps=int(body.get("steps") or 0), seed=int(body.get("seed") or 0),
            source_job_id=source_job_id, source_path=source_path, context=context,
            raw_request=prompt, engine_mode=str(body.get("engine_mode") or body.get("image_mode")
                                                or CORE.settings.get("image", "default_mode", "auto")))
    except ImageBackendUnavailable as exc:
        return _err(str(exc), 503)
    return _ok({"job": job}) if job.get("status") == "completed" else (
        {"ok": False, "error": job.get("error") or "Génération échouée.", "job": job}, 502)


# ---------------------------------------------------------------------------
# Pièces jointes (File & Image Analysis)
# ---------------------------------------------------------------------------
@router.get("/api/uploads/capabilities")
def api_uploads_capabilities(req):
    caps = CORE.attachments.capabilities()
    vision = CORE.llm.vision_status()
    caps.update({
        "max_attachments_per_message": int(CORE.settings.get("files", "max_attachments_per_message", 6)),
        "vision_available": bool(vision.get("available")),
        "vision_provider": str(vision.get("provider") or ""),
        "vision_model": str(vision.get("model") or ""),
    })
    return _ok(caps)


@router.get("/api/uploads")
def api_uploads_list(req):
    return _ok({"attachments": [a.public() for a in CORE.attachments.list()]})


@router.post("/api/uploads")
def api_uploads_create(req):
    import base64

    body = req["body"]
    attachments = body.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return _err("attachments requis (liste).")
    created = []
    errors = []
    for item in attachments:
        if not isinstance(item, dict):
            errors.append({"error": "élément invalide"})
            continue
        filename = str(item.get("filename") or "").strip()
        data_b64 = str(item.get("data_b64") or "").strip()
        if not data_b64:
            errors.append({"filename": filename, "error": "data_b64 manquant"})
            continue
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception:
            errors.append({"filename": filename, "error": "Données base64 invalides."})
            continue
        try:
            att = CORE.attachments.store(filename, raw)
            created.append(att.public())
        except AttachmentError as exc:
            errors.append({"filename": filename, "error": str(exc)})
    return _ok({"attachments": created, "errors": errors})


@router.post("/api/uploads/ingest")
def api_uploads_ingest(req):
    path = str(req["body"].get("path") or "").strip()
    filename = str(req["body"].get("filename") or "").strip()
    if not path:
        return _err("path requis.")
    try:
        att = CORE.attachments.ingest_file(path, filename)
    except AttachmentError as exc:
        return _err(str(exc), 400)
    return _ok({"attachment": att.public()})


@router.get("/api/uploads/<att_id>")
def api_uploads_detail(req, att_id):
    att = CORE.attachments.resolve([att_id])
    if not att:
        return _err("Pièce jointe introuvable.", 404)
    data = att.public()
    vox = CORE.llm.vision_status()
    data["vision_available"] = bool(vox.get("available"))
    data["extract"] = None
    return _ok(data)


@router.get("/api/uploads/<att_id>/file")
def api_uploads_file(req, att_id):
    found = CORE.attachments.serve(att_id)
    if found is None:
        return _err("Fichier introuvable.", 404)
    data, ctype = found
    return RawResponse(data, ctype)


@router.get("/api/uploads/<att_id>/thumb")
def api_uploads_thumb(req, att_id):
    thumb = CORE.attachments.thumbnail(att_id)
    if thumb is None:
        return _err("Vignette indisponible.", 404)
    return RawResponse(thumb, "image/png")


@router.delete("/api/uploads/<att_id>")
def api_uploads_delete(req, att_id):
    if not CORE.attachments.delete(att_id):
        return _err("Pièce jointe introuvable.", 404)
    return _ok({"deleted": att_id})


# ---------------------------------------------------------------------------
# Atelier 3D Blender
# ---------------------------------------------------------------------------
@router.get("/api/blender/status")
def api_blender_status(req):
    return _ok(CORE.blender.status())


@router.post("/api/blender/test")
def api_blender_test(req):
    """Lance réellement Blender et vérifie qu'il répond (bouton Tester)."""
    result = CORE.blender.test()
    return _ok({"test": result}) if result.get("ok") else (
        {"ok": False, "error": result.get("error") or "Blender n'a pas répondu.",
         "test": result}, 502)


@router.get("/api/blender/jobs")
def api_blender_jobs(req):
    conversation_id = req["query"].get("conversation_id", [""])[0]
    limit = int(req["query"].get("limit", ["50"])[0])
    return _ok({"jobs": CORE.blender.list(conversation_id, limit=limit),
                "current": CORE.blender.current(conversation_id),
                "available": CORE.blender.available()})


@router.get("/api/blender/jobs/<job_id>")
def api_blender_job(req, job_id):
    job = CORE.blender.get(job_id)
    if not job:
        return _err("Job 3D inconnu.", 404)
    return _ok({"job": job})


@router.get("/api/blender/jobs/<job_id>/files/<filename>")
def api_blender_job_file(req, job_id, filename):
    found = CORE.blender.file(job_id, filename)
    if not found:
        return _err("Fichier 3D introuvable.", 404)
    data, ctype = found
    return RawResponse(data, ctype)


@router.post("/api/blender/jobs/<job_id>/cancel")
def api_blender_job_cancel(req, job_id):
    job = CORE.blender.cancel(job_id)
    if not job:
        return _err("Job 3D inconnu.", 404)
    return _ok({"job": job})


@router.get("/api/blender/projects")
def api_blender_projects(req):
    conversation_id = req["query"].get("conversation_id", [""])[0]
    return _ok({"projects": CORE.blender.list_projects(conversation_id),
                "current": CORE.blender.current(conversation_id)})


@router.post("/api/blender/run")
def api_blender_run(req):
    """Exécution directe d'un outil 3D (UI, tests) — même chaîne que le LLM."""
    body = req["body"]
    tool_id = str(body.get("tool") or "blender.create_model")
    if not tool_id.startswith("blender."):
        return _err("Outil 3D invalide.")
    result = CORE.runner.run(tool_id, dict(body.get("arguments") or {}),
                             conversation_id=str(body.get("conversation_id") or ""))
    payload = result.to_dict()
    return _ok(payload) if result.ok else ({"ok": False, "error": result.output,
                                            **payload}, 502)


# ---------------------------------------------------------------------------
# Avatar 3D — mise à jour depuis image / par référence
# ---------------------------------------------------------------------------
@router.post("/api/avatar/references")
def api_avatar_ref_add(req):
    """Ajoute une image de référence (fichier local sur le serveur)."""
    body = req["body"]
    path = str(body.get("path") or "").strip()
    if not path:
        return _err("Chemin d'image manquant.")
    try:
        ref = CORE.avatar_ref.add(
            path,
            reference_type=str(body.get("reference_type") or "mixed"),
            tags=body.get("tags") or [],
            conversation_id=str(body.get("conversation_id") or ""))
    except ValueError as exc:
        return _err(str(exc))
    return _ok({"reference": ref})


@router.post("/api/avatar/references/upload")
def api_avatar_ref_upload(req):
    """Accepte une image encodée en base64 (upload navigateur) et l'enregistre.

    Corps JSON : {"filename": "photo.png", "data_b64": "<base64>",
                  "reference_type": "face", "conversation_id": ""}
    """
    import base64
    from .avatar_reference import REFS_DIR
    body = req["body"]
    filename = str(body.get("filename") or "").strip()
    data_b64 = str(body.get("data_b64") or "").strip()
    if not filename or not data_b64:
        return _err("filename et data_b64 requis.")
    ext = Path(filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return _err("Format d'image non supporté : " + (ext or "(inconnu)"))
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        return _err("Données base64 invalides.")
    if not raw:
        return _err("Image vide.")
    if len(raw) > 4_000_000:
        return _err("Image trop volumineuse (max 4 Mo).")
    safe = "".join(ch for ch in Path(filename).name if ch.isalnum() or ch in "._-")
    if not safe:
        safe = "reference" + ext
    target = REFS_DIR / f"{int(time.time() * 1000)}_{safe}"
    try:
        target.write_bytes(raw)
    except OSError as exc:
        return _err(f"Enregistrement impossible : {exc}")
    try:
        ref = CORE.avatar_ref.add(
            str(target),
            reference_type=str(body.get("reference_type") or "mixed"),
            tags=body.get("tags") or [],
            conversation_id=str(body.get("conversation_id") or ""))
    except ValueError as exc:
        target.unlink(missing_ok=True)
        return _err(str(exc))
    return _ok({"reference": ref})


@router.get("/api/avatar/references")
def api_avatar_refs(req):
    from .avatar_reference import REFERENCE_TYPES
    limit = int(req["query"].get("limit", ["30"])[0])
    return _ok({"references": CORE.avatar_ref.list(limit=limit),
                "available_types": list(REFERENCE_TYPES)})


# ---------------------------------------------------------------------------
# AvatarEngine — identité contrôlée CURRENT / CANDIDATE / REFERENCE
# ---------------------------------------------------------------------------
@router.get("/api/avatar/engine/status")
def api_avatar_engine_status(req):
    return _ok(CORE.avatar_engine.status())


@router.get("/api/avatar/engine/comparison")
def api_avatar_engine_comparison(req):
    """Source unique pour le panneau de comparaison Avatar Studio."""
    comparison = CORE.avatar_engine.comparison()
    ref_id = req["query"].get("reference_id", [""])[0]
    if ref_id and CORE.avatar_ref.get(ref_id):
        comparison["reference_url"] = f"/api/avatar/references/{ref_id}/image"
    else:
        comparison["reference_url"] = ""
    comparison["build"] = CORE.avatar_engine.status()["build"]
    return _ok(comparison)


@router.get("/api/avatar/engine/candidate/model")
def api_avatar_engine_candidate_model(req):
    from .avatar_engine import identity
    candidate = identity.candidate_glb()
    if not candidate.is_file():
        return _err("Aucun candidat GLB disponible.", 404)
    return RawResponse(candidate.read_bytes(), "model/gltf-binary")


@router.delete("/api/avatar/references/<ref_id>")
def api_avatar_ref_delete(req, ref_id):
    return _ok({"deleted": CORE.avatar_ref.delete(ref_id)})


@router.get("/api/avatar/references/<ref_id>/image")
def api_avatar_ref_image(req, ref_id):
    """Sert l'image de référence elle-même (aperçu dans l'UI)."""
    ref = CORE.avatar_ref.get(ref_id)
    if not ref:
        return _err("Référence introuvable.", 404)
    src = Path(ref.get("source_path") or "")
    if not src.is_file():
        return _err("Fichier image introuvable.", 404)
    ctype = mimetypes.guess_type(str(src))[0] or "image/png"
    return RawResponse(src.read_bytes(), ctype)


@router.post("/api/avatar/references/<ref_id>/analyze")
def api_avatar_ref_analyze(req, ref_id):
    try:
        features = CORE.avatar_ref.analyze(ref_id)
    except ValueError as exc:
        return _err(str(exc))
    return _ok({"features": features})


@router.post("/api/avatar/update")
def api_avatar_update(req):
    """Lance le pipeline complet de mise à jour d'avatar depuis une référence."""
    body = req["body"]
    ref_id = str(body.get("reference_id") or "").strip()
    if not ref_id:
        return _err("reference_id manquant.")
    ref = CORE.avatar_ref.get(ref_id)
    if not ref:
        return _err("Référence inconnue.", 404)
    options = dict(body.get("options") or {})
    if not options:
        from .avatar_update import AvatarUpdateOptions
        options = AvatarUpdateOptions.from_text(
            str(body.get("instruction") or "")).to_dict()
    if not ref.get("extracted_features"):
        CORE.avatar_ref.analyze(ref_id)
    if body.get("background"):
        import threading
        result_holder = {}

        def _run():
            result_holder["result"] = CORE.avatar_pipeline.update_from_reference(
                ref_id, options=options,
                conversation_id=str(body.get("conversation_id") or ""),
                max_iterations=int(body.get("max_iterations") or 3))
            CORE.events.emit("avatar.update_completed", {
                "reference_id": ref_id,
                **result_holder["result"]})

        threading.Thread(target=_run, daemon=True,
                         name="avatar-update-pipeline").start()
        return _ok({"started": True, "reference_id": ref_id})
    result = CORE.avatar_pipeline.update_from_reference(
        ref_id, options=options,
        conversation_id=str(body.get("conversation_id") or ""),
        max_iterations=int(body.get("max_iterations") or 3))
    return _ok(result) if result.get("ok") else (
        {"ok": False, "error": result.get("error", "Échec inconnu."), **result}, 502)


@router.get("/api/avatar/revisions")
def api_avatar_revisions(req):
    ref_id = req["query"].get("reference_id", [""])[0]
    limit = int(req["query"].get("limit", ["20"])[0])
    revs = CORE.avatar_ref.list_revisions(reference_id=ref_id, limit=limit)
    active = CORE.avatar_ref.active_revision()
    return _ok({"revisions": revs, "active": active})


@router.get("/api/avatar/revisions/<rev_id>")
def api_avatar_revision(req, rev_id):
    rev = CORE.avatar_ref.get_revision(rev_id)
    return _ok({"revision": rev}) if rev else _err("Révision introuvable.", 404)


@router.post("/api/avatar/revisions/<rev_id>/accept")
def api_avatar_revision_accept(req, rev_id):
    ok = CORE.avatar_ref.accept_revision(rev_id)
    return _ok({"accepted": ok, "active": CORE.avatar_ref.active_revision()}) \
        if ok else _err("Révision introuvable.", 404)


@router.post("/api/avatar/revisions/<rev_id>/rollback")
def api_avatar_revision_rollback(req, rev_id):
    ok = CORE.avatar_ref.rollback(rev_id)
    return _ok({"rolled_back": ok}) if ok else _err("Révision introuvable.", 404)


@router.get("/api/avatar/revisions/<rev_id>/files/<filename>")
def api_avatar_revision_file(req, rev_id, filename):
    """Sert un fichier associé à une révision (preview PNG)."""
    rev = CORE.avatar_ref.get_revision(rev_id)
    if not rev:
        return _err("Révision introuvable.", 404)
    import mimetypes
    from pathlib import Path
    from .avatar_update import RENDERS_DIR
    candidate_map = {
        "preview_front.png": rev.get("preview_front", ""),
        "preview_side.png": rev.get("preview_side", ""),
        "preview_34.png": rev.get("preview_34", ""),
        "preview_full.png": rev.get("preview_full", ""),
    }
    target = candidate_map.get(filename, "")
    if not target or not Path(target).is_file():
        guess = Path(RENDERS_DIR) / rev_id / filename
        if guess.is_file():
            target = str(guess)
        else:
            return _err("Fichier introuvable.", 404)
    data = Path(target).read_bytes()
    ctype = mimetypes.guess_type(target)[0] or "image/png"
    return RawResponse(data, ctype)


# ---------------------------------------------------------------------------
# Avatar Studio LIVE — jobs observables en direct
# ---------------------------------------------------------------------------
_AVATAR_ASSET_TYPES = {".glb": "model/gltf-binary", ".png": "image/png"}


def _avatar_asset(job_id: str, kind: str, name: str):
    """Sert un asset live d'un job. Jamais un fichier en cours d'écriture :
    le worker Blender publie par rename atomique."""
    path = CORE.avatar_live.asset(job_id, kind, name)
    if path is None:
        return _err("Aperçu indisponible.", 404)
    try:
        data = path.read_bytes()
    except OSError:
        return _err("Aperçu momentanément illisible.", 404)
    ctype = _AVATAR_ASSET_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return RawResponse(data, ctype)


@router.get("/api/avatar/jobs")
def api_avatar_jobs(req):
    limit = int((req["query"].get("limit", ["20"])[0]) or 20)
    return _ok({"jobs": CORE.avatar_live.list(limit=limit),
                "active": CORE.avatar_live.active()})


@router.post("/api/avatar/jobs")
def api_avatar_job_start(req):
    body = req["body"] or {}
    result = CORE.avatar_live.start(
        str(body.get("reference_id") or ""),
        options=body.get("options") or {},
        quality=str(body.get("quality") or "balanced"),
        conversation_id=str(body.get("conversation_id") or ""),
        title=str(body.get("title") or ""))
    return _ok(result) if result.get("ok") else _err(result.get("error", "Échec."), 400)


@router.get("/api/avatar/jobs/<job_id>")
def api_avatar_job(req, job_id):
    job = CORE.avatar_live.get(job_id)
    return _ok({"job": job}) if job else _err("Job inconnu.", 404)


@router.get("/api/avatar/jobs/<job_id>/live/<name>")
def api_avatar_job_live(req, job_id, name):
    return _avatar_asset(job_id, "live", name)


@router.get("/api/avatar/jobs/<job_id>/before/<name>")
def api_avatar_job_before(req, job_id, name):
    return _avatar_asset(job_id, "before", name)


@router.get("/api/avatar/jobs/<job_id>/final/<name>")
def api_avatar_job_final(req, job_id, name):
    return _avatar_asset(job_id, "final", name)


@router.get("/api/avatar/jobs/<job_id>/revisions/<version>/<name>")
def api_avatar_job_revision(req, job_id, version, name):
    return _avatar_asset(job_id, "revision", f"{version}/{name}")


@router.post("/api/avatar/jobs/<job_id>/pause")
def api_avatar_job_pause(req, job_id):
    res = CORE.avatar_live.pause(job_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


@router.post("/api/avatar/jobs/<job_id>/resume")
def api_avatar_job_resume(req, job_id):
    res = CORE.avatar_live.resume(job_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


@router.post("/api/avatar/jobs/<job_id>/cancel")
def api_avatar_job_cancel(req, job_id):
    res = CORE.avatar_live.cancel(job_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


@router.post("/api/avatar/jobs/<job_id>/accept")
def api_avatar_job_accept(req, job_id):
    res = CORE.avatar_live.accept(job_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


@router.post("/api/avatar/jobs/<job_id>/reject")
def api_avatar_job_reject(req, job_id):
    res = CORE.avatar_live.reject(job_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


@router.post("/api/avatar/jobs/<job_id>/adjust")
def api_avatar_job_adjust(req, job_id):
    res = CORE.avatar_live.adjust(job_id, str((req["body"] or {}).get("text") or ""))
    return _ok(res) if res.get("ok") else _err(res.get("error", "Échec."), 400)


# ---------------------------------------------------------------------------
# Avatar 3D — présence corporelle
# ---------------------------------------------------------------------------
@router.get("/api/avatar")
def api_avatar(req):
    return _ok(CORE.avatar.snapshot())


@router.post("/api/avatar/command")
def api_avatar_command(req):
    """Commandes de mise en scène. Le frontend les exécute via le bus SSE."""
    body = req["body"]
    action = str(body.get("action") or "").lower()
    director = CORE.avatar

    if action == "move":
        ok = director.move_to(str(body.get("place") or ""),
                              reason=str(body.get("reason") or "api"))
        return _ok({"applied": ok}) if ok else _err("Emplacement inconnu ou locomotion désactivée.")
    if action == "look":
        ok = director.look_at(str(body.get("place") or ""),
                              reason=str(body.get("reason") or "api"))
        return _ok({"applied": ok}) if ok else _err("Emplacement inconnu.")
    if action == "gesture":
        ok = director.gesture(str(body.get("gesture") or ""),
                              emotion=str(body.get("emotion") or ""),
                              intensity=float(body.get("intensity") or 0.5),
                              reason=str(body.get("reason") or "api"))
        return _ok({"applied": ok})
    if action == "view":
        ok = director.set_view(str(body.get("view") or ""))
        return _ok({"applied": ok}) if ok else _err("Vue inconnue.")
    if action == "state":
        director.set_state(str(body.get("state") or "IDLE").upper(),
                           reason=str(body.get("reason") or "api"))
        return _ok({"applied": True})
    return _err("Action inconnue.")


@router.post("/api/avatar/viseme")
def api_avatar_viseme(req):
    """Le frontend publie la progression réelle du lip sync sur le bus."""
    body = req["body"]
    CORE.events.emit("tts.viseme", {
        "viseme": str(body.get("viseme") or "")[:12],
        "weight": float(body.get("weight") or 0),
        "index": int(body.get("index") or 0),
    })
    return _ok()


# ---------------------------------------------------------------------------
# Live Browser Preview — session Playwright partagée avec l'agent
# ---------------------------------------------------------------------------
@router.get("/api/browser/status")
def api_browser_status(req):
    return _ok(CORE.browser.status())


@router.get("/api/browser/frame")
def api_browser_frame(req):
    """Dernière frame JPEG (fallback de rafraîchissement si le SSE décroche).

    L'appel vaut « aperçu visible » : il maintient le stream de frames actif,
    exactement comme /api/browser/touch.
    """
    snap = CORE.browser.frame_snapshot()
    if not snap:
        return _err("aucune frame disponible", 404)
    return _ok(snap)


@router.post("/api/browser/start")
def api_browser_start(req):
    url = str(req["body"].get("url") or "https://example.com")
    CORE.browser.start()
    out = CORE.browser.action("navigate", {"url": url})
    CORE.browser.touch()
    return _ok(out)


@router.post("/api/browser/action")
def api_browser_action(req):
    """Action navigateur : navigate, click, type, scroll, wait, back, forward…"""
    op = str(req["body"].get("op") or "navigate")
    args = dict(req["body"].get("args") or {})
    if not CORE.browser.available():
        return _err("playwright n'est pas installé (voir README)", 503)
    CORE.browser.start()
    CORE.browser.touch()
    out = CORE.browser.action(op, args)
    return _ok(out)


@router.post("/api/browser/touch")
def api_browser_touch(req):
    """L'aperçu est visible : maintient le stream de frames actif."""
    CORE.browser.touch()
    return _ok()


@router.post("/api/browser/close")
def api_browser_close(req):
    CORE.browser.start()
    out = CORE.browser.action("close")
    return _ok(out)


@router.post("/api/browser/user-action")
def api_browser_user_action(req):
    """L'utilisateur a résolu l'action requise (captcha, login…) : on reprend."""
    action = str(req["body"].get("action") or "")
    if action == "pause":
        message = str(req["body"].get("message") or "Action manuelle requise")
        return _ok(CORE.browser.request_user(message))
    if action == "resume":
        return _ok(CORE.browser.resume())
    return _err("action inconnue", 400)


# ---------------------------------------------------------------------------
# Brain Atlas / activité temps réel
# ---------------------------------------------------------------------------
@router.get("/api/brain")
def api_brain(req):
    query = req["query"].get("q", [""])[0]
    return _ok(CORE.brain.search(query))


@router.get("/api/brain/graph")
def api_brain_graph(req):
    limit = int(req["query"].get("limit", ["300"])[0])
    return _ok({"graph": CORE.brain.graph(limit=limit)})


@router.get("/api/brain/stats")
def api_brain_stats(req):
    return _ok(CORE.brain.stats())


@router.post("/api/brain/activity")
def api_brain_activity(req):
    """Point d'entrée pour les activités du frontend (audio level, listening…)."""
    CORE.events.emit("activity.trace", {
        "ts": time.time(),
        "kind": str(req["body"].get("kind") or "action")[:40],
        "title": str(req["body"].get("title") or "")[:200],
        "detail": str(req["body"].get("detail") or "")[:600],
        "state": str(req["body"].get("state") or ""),
    })
    return _ok()


# ---------------------------------------------------------------------------
# Speech sanitizer — version parlable d'une réponse UI
# ---------------------------------------------------------------------------
@router.post("/api/speech/sanitize")
def api_speech_sanitize(req):
    from .speech_sanitizer import sanitize_for_speech
    text = str(req["body"].get("text") or "")
    return _ok({"speech": sanitize_for_speech(text)})


# ---------------------------------------------------------------------------
# Handler HTTP
# ---------------------------------------------------------------------------
class JarvisHandler(BaseHTTPRequestHandler):
    server_version = f"JARVIS/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        if CORE and CORE.settings.get("developer", "debug", False):
            print(f"[HTTP] {fmt % args}", flush=True)

    # -- utilitaires -------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # 12 Mo : un enregistrement vocal encodé en base64 dépasse les 4 Mo
        # d'origine, et une lecture tronquée produirait un corps illégal.
        raw = self.rfile.read(min(length, 12_000_000))
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {}

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api"):
            handler, params = router.match(method, path)
            if handler is None:
                self._send_json({"ok": False, "error": "Endpoint inconnu."}, 404)
                return
            req = {"body": self._read_body() if method in {"POST", "PUT", "DELETE"} else {},
                   "query": parse_qs(parsed.query), "path": path,
                   "server_port": self.server.server_address[1]}
            try:
                result = handler(req, **params)
            except Exception as exc:
                if CORE and CORE.settings.get("developer", "debug", False):
                    traceback.print_exc()
                self._send_json({"ok": False, "error": str(exc)}, 500)
                return
            if isinstance(result, RawResponse):
                self.send_response(result.status)
                self.send_header("Content-Type", result.content_type)
                self.send_header("Content-Length", str(len(result.body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not getattr(self, "_head_only", False):
                    self.wfile.write(result.body)
                return
            if isinstance(result, tuple):
                payload, status = result
                self._send_json(payload, status)
            else:
                self._send_json(result)
            return
        if method == "GET":
            self._serve_static(path)
        else:
            self._send_json({"ok": False, "error": "Méthode non autorisée."}, 405)

    def do_HEAD(self) -> None:  # noqa: N802
        """HEAD est légitime (diagnostic, préchargement) : on le sert."""
        self._head_only = True
        try:
            self._dispatch("GET")
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path.rstrip("/") == "/api/events":
            self._serve_sse()
            return
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # -- SSE ---------------------------------------------------------------
    def _serve_sse(self) -> None:
        sid, q = CORE.events.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_write({"type": "system.info", "ts": time.time(),
                             "data": {"message": "flux connecté", "subscriber": sid}})
            # Rejoue un court historique pour peupler l'interface immédiatement.
            for event in CORE.events.history(limit=20):
                if event['type'].startswith('code.file.'):
                    continue
                self._sse_write(event)
            snapshot = CORE.documents.snapshot()
            for doc in sorted(snapshot['documents'], key=lambda d: d['document_id'] == snapshot['activeDocumentId']):
                self._sse_write({'type': 'code.file.opened', 'data': {**doc, 'restored': True}, 'ts': time.time()})
            last_ping = time.time()
            while True:
                try:
                    event = q.get(timeout=1.0)
                    self._sse_write(event)
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        # Commentaire SSE : garde la connexion vivante SANS
                        # produire d'événement applicatif (donc sans greeting).
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            CORE.events.unsubscribe(sid)

    def _sse_write(self, event: dict[str, Any]) -> None:
        # Pas de champ `event:` nommé : une trame nommée n'atteint QUE
        # addEventListener("<nom>") et ne déclenche jamais EventSource.onmessage,
        # qui est le point d'entrée unique du frontend (core.js → J.fire).
        # Le type réel voyage dans le JSON (`event.type`).
        payload = json.dumps(event, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- statique ----------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in {"/", ""} else path.lstrip("/")
        base = UI_DIR.resolve()
        candidate = (base / rel).resolve()
        if base != candidate and base not in candidate.parents:
            self._send_json({"ok": False, "error": "Accès refusé."}, 403)
            return
        if not candidate.is_file():
            # Un asset manquant doit renvoyer 404, JAMAIS index.html : un
            # module ES servi en text/html échoue silencieusement côté
            # navigateur et l'erreur devient impossible à diagnostiquer.
            if candidate.suffix.lower() in ASSET_SUFFIXES:
                self._send_json({"ok": False, "error": f"Fichier introuvable : {rel}"}, 404)
                return
            candidate = base / "index.html"     # SPA : toute route inconnue → index
            if not candidate.is_file():
                self._send_json({"ok": False, "error": "Interface introuvable."}, 404)
                return
        data = candidate.read_bytes()
        if candidate.name == "index.html":
            data = data.replace(
                b"</body>",
                b'<script src="/js/self_upgrades.js?v=JARVIS_SELF_UPGRADE_V1"></script></body>'
            )
        ctype, _ = mimetypes.guess_type(candidate.name)
        if candidate.suffix.lower() == ".js":
            ctype = "text/javascript"           # indépendant du registre Windows
        elif candidate.suffix.lower() == ".glb":
            ctype = "model/gltf-binary"
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # Application locale : on préfère toujours la version fraîche à un
        # module JS périmé dans le cache du navigateur.
        self.send_header("Cache-Control",
                         "no-store"
                         if candidate.suffix.lower() in {".js", ".css", ".html", ".glb"}
                         else "no-cache")
        self.end_headers()
        # Une réponse HEAD ne porte pas de corps : en écrire un désynchronise
        # la connexion keep-alive et fait échouer les requêtes suivantes.
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)


# ---------------------------------------------------------------------------
# CRM — contacts, sociétés, opportunités, historique
# ---------------------------------------------------------------------------
@router.get("/api/crm/contacts")
def api_crm_contacts(req):
    query = req["query"].get("q", [""])[0].strip()
    if query:
        return _ok({"contacts": CORE.crm.search(query, limit=50), "query": query})
    limit = int(req["query"].get("limit", ["200"])[0])
    return _ok({"contacts": CORE.crm.all(limit=limit)})


@router.get("/api/crm/contacts/<cid>")
def api_crm_contact_get(req, cid):
    """Fiche complète : contact, société, opportunités, historique, score."""
    view = CORE.crm_pipeline.contact_view(cid)
    return _ok(view) if view else _err("Contact introuvable.", 404)


@router.post("/api/crm/contacts")
def api_crm_contact_save(req):
    body = req["body"] or {}
    try:
        contact = CORE.crm.upsert(body)
    except Exception as exc:
        return _err(str(exc))
    # Les champs étendus ne passent pas par CrmStore (table pivot historique) :
    # on les écrit ici pour ne pas dupliquer la logique de `upsert`.
    extras = {k: body[k] for k in ("company_id", "role", "owner", "source", "status") if k in body}
    if extras:
        sets = ", ".join(f"{k}=?" for k in extras)
        CORE.db.execute(f"UPDATE crm_contacts SET {sets} WHERE id=?", (*extras.values(), contact["id"]))
    if "tags" in body:
        CORE.db.execute("UPDATE crm_contacts SET tags=? WHERE id=?",
                        (json.dumps(body.get("tags") or [], ensure_ascii=False), contact["id"]))
    CORE.audit.record(action="crm.contact.saved", connector_id="", detail={"id": contact["id"]})
    CORE.events.emit("crm.contact.saved", {"id": contact["id"], "name": contact.get("name", "")})
    return _ok(CORE.crm_pipeline.contact_view(contact["id"]))


@router.delete("/api/crm/contacts/<cid>")
def api_crm_contact_delete(req, cid):
    return _ok({"deleted": CORE.crm.delete(cid)})


@router.get("/api/crm/companies")
def api_crm_companies(req):
    return _ok({"companies": CORE.crm_pipeline.companies.all(
        limit=int(req["query"].get("limit", ["200"])[0]))})


@router.post("/api/crm/companies")
def api_crm_company_save(req):
    try:
        return _ok({"company": CORE.crm_pipeline.companies.upsert(req["body"] or {})})
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/api/crm/companies/<company_id>")
def api_crm_company_delete(req, company_id):
    return _ok({"deleted": CORE.crm_pipeline.companies.delete(company_id)})


@router.get("/api/crm/deals")
def api_crm_deals(req):
    return _ok({"deals": CORE.crm_pipeline.deals.list(
        contact_id=req["query"].get("contact_id", [""])[0],
        status=req["query"].get("status", [""])[0])})


@router.post("/api/crm/deals")
def api_crm_deal_save(req):
    try:
        deal = CORE.crm_pipeline.deals.upsert(req["body"] or {})
    except ValueError as exc:
        return _err(str(exc))
    if deal.get("contact_id"):
        CORE.crm_pipeline.scorer.apply(deal["contact_id"])   # le score suit l'opportunité
    CORE.events.emit("crm.deal.saved", {"id": deal["id"], "stage": deal["stage"], "status": deal["status"]})
    return _ok({"deal": deal})


@router.delete("/api/crm/deals/<deal_id>")
def api_crm_deal_delete(req, deal_id):
    return _ok({"deleted": CORE.crm_pipeline.deals.delete(deal_id)})


@router.get("/api/crm/pipeline")
def api_crm_pipeline(req):
    return _ok(CORE.crm_pipeline.deals.pipeline())


@router.get("/api/crm/interactions")
def api_crm_interactions(req):
    return _ok({"interactions": CORE.crm_pipeline.interactions.timeline(
        req["query"].get("contact_id", [""])[0],
        limit=int(req["query"].get("limit", ["100"])[0]))})


@router.post("/api/crm/interactions")
def api_crm_interaction_add(req):
    """Point d'entrée des agents : historique et score mis à jour ensemble."""
    try:
        return _ok(CORE.crm_pipeline.log_interaction(req["body"] or {}))
    except Exception as exc:
        return _err(str(exc))


@router.delete("/api/crm/interactions/<interaction_id>")
def api_crm_interaction_delete(req, interaction_id):
    """Un échange journalisé par erreur doit pouvoir être retiré.

    Le score du contact est recalculé dans la foulée : le laisser tel quel
    conserverait les points d'une interaction qui n'existe plus, et le score
    ne serait plus justifiable par son historique.
    """
    row = CORE.crm_pipeline.interactions.get(interaction_id)
    contact_id = (row or {}).get("contact_id") or ""
    deleted = CORE.crm_pipeline.interactions.delete(interaction_id)
    if deleted and contact_id:
        CORE.crm_pipeline.scorer.apply(contact_id)
    return _ok({"deleted": deleted})


@router.post("/api/crm/rescore")
def api_crm_rescore(req):
    contact_id = str((req["body"] or {}).get("contact_id") or "").strip()
    if contact_id:
        return _ok({"scoring": CORE.crm_pipeline.scorer.apply(contact_id)})
    return _ok({"rescored": CORE.crm_pipeline.scorer.rescore_all()})


# ---------------------------------------------------------------------------
# Coffre-fort d'identifiants
#
# RÈGLE DE CETTE SECTION : aucune valeur secrète ne traverse ces routes, à deux
# exceptions assumées et journalisées — /reveal, déclenché par l'utilisateur
# avec confirmation explicite, et le code TOTP, à usage unique et périmé en
# 30 secondes. Les agents, eux, n'obtiennent JAMAIS de secret par HTTP : ils
# appellent core.credentials.get_credentials() en processus (voir plus bas).
# ---------------------------------------------------------------------------
@router.get("/api/vault/credentials")
def api_vault_list(req):
    include = req["query"].get("include_revoked", ["0"])[0] in ("1", "true")
    return _ok({"credentials": CORE.credentials.list(include_revoked=include),
                "stats": CORE.credentials.stats()})


@router.get("/api/vault/credentials/<cred_id>")
def api_vault_get(req, cred_id):
    cred = CORE.credentials.get(cred_id)
    return _ok({"credential": cred}) if cred else _err("Fiche introuvable.", 404)


@router.post("/api/vault/credentials")
def api_vault_save(req):
    try:
        return _ok({"credential": CORE.credentials.upsert(req["body"] or {})})
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/api/vault/credentials/<cred_id>")
def api_vault_delete(req, cred_id):
    return _ok({"deleted": CORE.credentials.delete(cred_id)})


@router.post("/api/vault/credentials/<cred_id>/rotate")
def api_vault_rotate(req, cred_id):
    try:
        return _ok({"credential": CORE.credentials.rotate(cred_id, req["body"] or {})})
    except ValueError as exc:
        return _err(str(exc))


@router.post("/api/vault/credentials/<cred_id>/revoke")
def api_vault_revoke(req, cred_id):
    reason = str((req["body"] or {}).get("reason") or "")
    return _ok({"revoked": CORE.credentials.revoke(cred_id, reason=reason)})


@router.post("/api/vault/credentials/<cred_id>/grants")
def api_vault_grant(req, cred_id):
    body = req["body"] or {}
    try:
        grants = CORE.credentials.grant(
            cred_id, str(body.get("agent_id") or ""),
            scopes=body.get("scopes"), max_uses=int(body.get("max_uses") or 0),
            ttl_seconds=int(body.get("ttl_seconds") or 0),
            task_pattern=str(body.get("task_pattern") or ""), reason=str(body.get("reason") or ""))
        return _ok({"grants": grants})
    except ValueError as exc:
        return _err(str(exc))


@router.delete("/api/vault/credentials/<cred_id>/grants/<agent_id>")
def api_vault_grant_revoke(req, cred_id, agent_id):
    return _ok({"revoked": CORE.credentials.revoke_grant(cred_id, agent_id)})


@router.post("/api/vault/credentials/<cred_id>/reveal")
def api_vault_reveal(req, cred_id):
    """Révélation d'UN champ à l'utilisateur, sur confirmation explicite.

    Seule route qui renvoie une valeur en clair, et seulement pour l'humain
    devant l'écran : `confirm: true` est obligatoire (un appel accidentel ou
    une requête forgée sans ce champ ne révèle rien), l'accès est journalisé
    avec le champ concerné, et un seul champ est renvoyé à la fois.
    """
    body = req["body"] or {}
    if not body.get("confirm"):
        return _err("Confirmation explicite requise pour révéler un secret.", 403)
    field = str(body.get("field") or "").strip()
    if field not in ALL_SECRET_FIELDS:
        return _err(f"Champ inconnu : {field}", 400)
    cred = CORE.credentials.get(cred_id)
    if not cred:
        return _err("Fiche introuvable.", 404)
    value = CORE.vault.get(f"cred:{cred_id}", field)
    CORE.audit.record(action="vault.secret.revealed", status="warn", agent="ui", connector_id=cred_id,
                      detail={"service": cred["service_name"], "champ": field})
    CORE.events.emit("vault.secret.revealed", {"credential_id": cred_id, "field": field})
    return _ok({"field": field, "value": value})


@router.get("/api/vault/audit")
def api_vault_audit(req):
    return _ok({"entries": CORE.credentials.audit_entries(
        limit=int(req["query"].get("limit", ["100"])[0]),
        credential_id=req["query"].get("credential_id", [""])[0])})


@router.post("/api/vault/sweep")
def api_vault_sweep(req):
    return _ok({"expired": CORE.credentials.sweep_expired()})


# ---------------------------------------------------------------------------
# Interface agent
#
# Écart ASSUMÉ par rapport à une API get_credentials classique : cette route ne
# renvoie PAS de mot de passe ni de clé. Les agents de JARVIS s'exécutent dans
# le même processus que le coffre ; leur faire transiter des secrets par HTTP
# créerait une surface d'exfiltration accessible à n'importe quel programme
# local, sans aucun bénéfice. La route sert donc au contrôle d'habilitation et
# au TOTP ; le secret lui-même s'obtient en processus :
#
#     from jarvis.vault_credentials import AgentContext
#     bundle = core.credentials.get_credentials(cred_id, AgentContext(
#         agent_id="web_agent", task_id=task.id, target_url=url))
#     page.fill("#password", bundle.password)
# ---------------------------------------------------------------------------
@router.post("/api/agent/credentials/get")
def api_agent_credentials(req):
    body = req["body"] or {}
    ctx = AgentContext(
        agent_id=str(body.get("agent_id") or ""), task_id=str(body.get("task_id") or ""),
        tool=str(body.get("tool") or ""), purpose=str(body.get("purpose") or ""),
        target_url=str(body.get("target_url") or ""))
    cred_id = str(body.get("credential_id") or "")
    scope = str(body.get("scope") or "login")
    try:
        if not CORE.credentials.authorize(cred_id, ctx, scope):
            # Rejoue la vérification pour obtenir le motif exact ET le journaliser.
            CORE.credentials.get_credentials(cred_id, ctx, scope=scope)
        cred = CORE.credentials.get(cred_id) or {}
        payload = {
            "authorized": True, "credential_id": cred_id,
            "service_name": cred.get("service_name", ""), "service_url": cred.get("service_url", ""),
            "auth_type": cred.get("auth_type", ""),
            "fields": sorted(cred.get("secret_fields", {})),
            "has_totp": cred.get("has_totp", False),
            "hint": "Secrets accessibles en processus via core.credentials.get_credentials().",
        }
        if body.get("totp") and cred.get("has_totp"):
            payload["totp"] = CORE.credentials.totp_code(cred_id, ctx)
        return _ok(payload)
    except VaultDenied as denied:
        return {"ok": False, "authorized": False, "error": str(denied)}, 403


def create_server(core, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    global CORE
    CORE = core
    # Windows SO_REUSEADDR permits two HTTP servers on the same endpoint.
    # Exclusive binding prevents mixed runtimes/builds on port 8765.
    import socket
    server = ThreadingHTTPServer((host, port), JarvisHandler, bind_and_activate=False)
    server.allow_reuse_address = False
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.server_bind()
        server.server_activate()
    except Exception:
        server.server_close()
        raise
    server.daemon_threads = True
    return server
