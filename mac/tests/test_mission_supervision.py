"""MISSION CONTROL V2 : supervision exploitable.

Persistance, historique filtrable, REPLAY, diagnostic d'échec et santé réelle
des composants. Ces tests utilisent une VRAIE base SQLite (schéma complet du
projet) : c'est le seul moyen de prouver qu'un redémarrage ne perd pas
l'historique.
"""
import os
import tempfile
import time
import unittest

from jarvis.db import Database
from jarvis.mission_control import (DEGRADED, OFFLINE, ONLINE, UNKNOWN,
                                    MissionControl, health_report)
from jarvis.mission_store import MissionStore
from tests.test_mission_control import FakeBus


def fresh_db():
    return Database(os.path.join(tempfile.mkdtemp(), "missions.db"))


class Base(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.bus = FakeBus()
        self.mc = MissionControl(self.bus, self.db)

    def run_mission(self, tid, name, *, fail=False, agent="jarvis", tools=("fs.read",)):
        self.bus.emit("task.created", {"id": tid, "name": name, "agent": agent})
        self.bus.emit("task.started", {"id": tid, "name": name, "agent": agent})
        self.bus.emit("task.progress", {"id": tid, "plan": [
            {"key": "a", "label": "Préparation", "state": "done"},
            {"key": "b", "label": "Exécution", "state": "run"}]})
        for tool in tools:
            self.bus.emit("tool.called", {"tool": tool, "name": tool, "task_id": tid,
                                          "agent": agent, "args": {"path": "/tmp/x", "token": "SECRET"}})
            self.bus.emit("tool.completed" if not fail else "tool.failed",
                          {"tool_id": tool, "name": tool, "task_id": tid, "ok": not fail,
                           "duration_ms": 120, "preview": "ok",
                           "error": "" if not fail else "Fichier introuvable"})
        self.bus.emit("code.file.opened", {"path": "/var/www/index.php", "task_id": tid})
        if fail:
            self.bus.emit("task.failed", {"id": tid, "error": "Connexion SSH refusée."})
        else:
            self.bus.emit("task.completed", {"id": tid, "result": "Terminé."})
        return tid


# ---------------------------------------------------------------- persistance
class PersistenceTests(Base):
    def test_a_finished_mission_survives_a_restart(self):
        self.run_mission("task_1", "Analyse du marketplace")
        # Un redémarrage : nouveau bus, nouveau MissionControl, MÊME base.
        restarted = MissionControl(FakeBus(), self.db)
        mission = restarted.detail("task_1")
        self.assertIsNotNone(mission)
        self.assertEqual(mission["name"], "Analyse du marketplace")
        self.assertEqual(mission["state"], "COMPLETED")
        self.assertEqual(mission["agent"], "jarvis")
        self.assertIn("fs.read", mission["tools"])
        self.assertEqual(mission["steps_total"], 2)
        self.assertGreaterEqual(mission["tool_calls"], 1)

    def test_every_required_field_is_stored(self):
        self.run_mission("task_1", "Mission", fail=True)
        row = MissionStore(self.db).get("task_1")
        for key in ("id", "name", "agent", "status", "started_at", "completed_at",
                    "duration", "tools", "steps_total", "error"):
            self.assertIn(key, row, key)
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["error"], "Connexion SSH refusée.")
        self.assertIsNotNone(row["completed_at"])

    def test_an_interrupted_mission_is_never_left_running(self):
        """Un RUNNING éternel après un crash ferait chercher une exécution
        qui n'existe plus : on dit la vérité, elle a été interrompue."""
        self.bus.emit("task.created", {"id": "task_x", "name": "Coupée", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": "task_x", "name": "Coupée", "agent": "jarvis"})
        restarted = MissionControl(FakeBus(), self.db)
        mission = restarted.detail("task_x")
        self.assertEqual(mission["state"], "FAILED")
        self.assertIn("redémarrage", mission["error"])

    def test_big_payloads_are_not_stored(self):
        tid = "task_big"
        self.bus.emit("task.created", {"id": tid, "name": "Grosse", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": tid, "name": "Grosse", "agent": "jarvis"})
        self.bus.emit("tool.called", {"tool": "fs.write", "task_id": tid,
                                      "args": {"content": "x" * 50000}})
        self.bus.emit("tool.completed", {"tool_id": "fs.write", "task_id": tid, "ok": True,
                                         "preview": "y" * 50000})
        self.bus.emit("task.completed", {"id": tid, "result": "z" * 50000})
        row = MissionStore(self.db).get(tid)
        self.assertLessEqual(len(row["result"]), 2000)
        for entry in MissionStore(self.db).timeline(tid):
            self.assertLessEqual(len(entry["detail"]), 400)

    def test_a_crash_mid_mission_keeps_what_already_happened(self):
        """Sans point de reprise, une mission longue perdrait toute sa
        timeline en cas d'arret brutal — c'est justement celle qu'on veut
        pouvoir relire."""
        tid = "task_crash"
        self.bus.emit("task.created", {"id": tid, "name": "Longue", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": tid, "name": "Longue", "agent": "jarvis"})
        for i in range(6):
            self.bus.emit("tool.called", {"tool": f"t{i}.run", "task_id": tid, "args": {}})
            self.bus.emit("tool.completed", {"tool_id": f"t{i}.run", "task_id": tid,
                                             "ok": True, "duration_ms": 10, "preview": "ok"})
        for i in range(30):
            self.bus.emit("task.progress", {"id": tid, "log": {"level": "info", "message": f"l{i}"}})
        # Arret brutal : aucun task.completed/failed n'arrive jamais.
        restarted = MissionControl(FakeBus(), self.db)
        mission = restarted.detail(tid)
        self.assertEqual(mission["state"], "FAILED")
        self.assertIn("redémarrage", mission["error"])
        self.assertTrue(restarted.timeline(tid), "la timeline deja vecue doit etre relisible")
        self.assertIn("t0.run", mission["tools"])

    def test_history_is_bounded_on_disk(self):
        store = MissionStore(self.db)
        for i in range(40):
            store.save({"id": f"m{i}", "name": f"M{i}", "state": "COMPLETED",
                        "created_at": time.time() + i, "duration": 1})
            store.append_events(f"m{i}", [{"kind": "log", "label": "x", "ts": time.time()}], time.time())
        removed = store.prune(keep=10)
        self.assertEqual(removed, 30)
        self.assertEqual(len(store.list("ALL", limit=200)), 10)
        left = self.db.scalar("SELECT COUNT(*) FROM mission_events")
        self.assertEqual(left, 10)

    def test_a_mission_burst_does_not_write_a_row_per_log(self):
        tid = "task_chatty"
        self.bus.emit("task.created", {"id": tid, "name": "Bavarde", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": tid, "name": "Bavarde", "agent": "jarvis"})
        for i in range(600):
            self.bus.emit("task.progress", {"id": tid, "log": {"level": "info", "message": f"m{i}"}})
        self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        rows = self.db.scalar("SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (tid,))
        self.assertLessEqual(rows, 400, "la timeline persistée doit rester bornée")
        self.assertGreater(rows, 0)


# ------------------------------------------------------------------ historique
class HistoryTests(Base):
    def setUp(self):
        super().setUp()
        self.run_mission("task_ok", "Terminée")
        self.run_mission("task_ko", "Échouée", fail=True)
        self.bus.emit("task.created", {"id": "task_live", "name": "En cours", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": "task_live", "name": "En cours", "agent": "jarvis"})

    def names(self, status):
        return [m["name"] for m in self.mc.history(status)["missions"]]

    def test_all_filter_returns_every_mission(self):
        names = self.names("ALL")
        for expected in ("En cours", "Terminée", "Échouée"):
            self.assertIn(expected, names)

    def test_running_filter_returns_only_live_missions(self):
        self.assertEqual(self.names("RUNNING"), ["En cours"])

    def test_completed_and_failed_filters(self):
        self.assertEqual(self.names("COMPLETED"), ["Terminée"])
        self.assertEqual(self.names("FAILED"), ["Échouée"])

    def test_counts_reflect_the_real_rows(self):
        counts = self.mc.history("ALL")["counts"]
        self.assertEqual(counts["COMPLETED"], 1)
        self.assertEqual(counts["FAILED"], 1)
        self.assertGreaterEqual(counts["RUNNING"], 1)

    def test_a_live_mission_is_not_listed_twice(self):
        ids = [m["id"] for m in self.mc.history("ALL")["missions"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_an_unknown_filter_falls_back_to_all(self):
        self.assertEqual(self.mc.history("N'IMPORTE QUOI")["status"], "ALL")

    def test_an_old_mission_detail_is_complete(self):
        restarted = MissionControl(FakeBus(), self.db)
        mission = restarted.detail("task_ko")
        self.assertEqual(mission["agent"], "jarvis")
        self.assertTrue(mission["files"])
        self.assertTrue(mission["steps"])
        self.assertTrue(mission["timeline"])
        self.assertGreater(mission["duration"], -1)


# ---------------------------------------------------------------------- replay
class ReplayTests(Base):
    def test_timeline_is_chronological_with_offsets(self):
        self.run_mission("task_1", "Mission")
        timeline = self.mc.timeline("task_1")
        self.assertTrue(timeline)
        offsets = [e["offset_ms"] for e in timeline]
        self.assertEqual(offsets, sorted(offsets), "le replay doit être chronologique")
        self.assertGreaterEqual(offsets[0], 0)

    def test_timeline_survives_a_restart(self):
        self.run_mission("task_1", "Mission")
        restarted = MissionControl(FakeBus(), self.db)
        self.assertTrue(restarted.timeline("task_1"))

    def test_timeline_carries_what_the_replay_displays(self):
        self.run_mission("task_1", "Mission", fail=True)
        kinds = {e["kind"] for e in self.mc.timeline("task_1")}
        self.assertIn("mission", kinds)
        self.assertIn("tool", kinds)
        self.assertIn("tool_result", kinds)
        failed = [e for e in self.mc.timeline("task_1") if e["ok"] is False]
        self.assertTrue(failed)
        self.assertEqual(failed[0]["tool"], "fs.read")

    def test_reading_a_timeline_executes_nothing(self):
        """Le REPLAY est une relecture : il ne doit produire AUCUN événement."""
        self.run_mission("task_1", "Mission")
        before = len(self.bus.emitted)
        self.mc.timeline("task_1")
        self.mc.detail("task_1")
        self.assertEqual(len(self.bus.emitted), before)

    def test_timeline_of_an_unknown_mission_is_empty(self):
        self.assertEqual(self.mc.timeline("task_inconnue"), [])


# ------------------------------------------------------------------ diagnostic
class DiagnosticTests(Base):
    def test_failure_diagnostic_answers_the_four_questions(self):
        self.run_mission("task_ko", "Publication Discord", fail=True, agent="BrainrotFortniteAgent")
        text = self.mc.diagnostic("task_ko")
        self.assertIn("Publication Discord", text)
        self.assertIn("BrainrotFortniteAgent", text)   # quel agent
        self.assertIn("fs.read", text)                 # quel outil
        self.assertIn("Connexion SSH refusée.", text)  # quel message
        self.assertIn("ÉCHEC", text)
        self.assertIn("Étape", text)                   # où
        self.assertIn("00:00:", text)                  # durée avant erreur

    def test_no_secret_ever_reaches_the_diagnostic(self):
        tid = "task_secret"
        self.bus.emit("task.created", {"id": tid, "name": "Connexion", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": tid, "name": "Connexion", "agent": "jarvis"})
        self.bus.emit("tool.called", {"tool": "ssh.run", "task_id": tid, "args": {
            "password": "hunter2", "api_key": "sk-ultra-secret", "token": "tok-123",
            "authorization": "Bearer abcdef", "host": "prod.example.com"}})
        self.bus.emit("task.failed", {"id": tid, "error": "Auth refusée"})
        text = self.mc.diagnostic(tid)
        for secret in ("hunter2", "sk-ultra-secret", "tok-123", "Bearer abcdef"):
            self.assertNotIn(secret, text, f"secret divulgué : {secret}")
        self.assertIn("prod.example.com", text)  # le contexte utile reste

    def test_diagnostic_is_structured_in_named_sections(self):
        """Un diagnostic se lit en diagonale : chaque information a son titre."""
        self.run_mission("task_ko", "Publication Discord", fail=True,
                         agent="BrainrotFortniteAgent")
        text = self.mc.diagnostic("task_ko")
        for section in ("MISSION", "AGENT", "STATUT", "DURATION", "STEP", "TOOL",
                        "ERROR", "STEPS", "FILES", "LAST EVENTS"):
            self.assertIn(section, text, section)

    def test_the_error_comes_before_the_context(self):
        """L'erreur est ce qu'on cherche : elle ne doit pas être enterrée."""
        self.run_mission("task_ko", "Mission", fail=True)
        text = self.mc.diagnostic("task_ko")
        self.assertLess(text.index("ERROR"), text.index("CONTEXTE"))
        self.assertLess(text.index("ERROR"), text.index("LAST EVENTS"))
        self.assertLess(text.index("Connexion SSH refusée."), text.index("CONTEXTE"))

    def test_a_successful_mission_has_no_error_section(self):
        self.run_mission("task_ok", "Mission")
        text = self.mc.diagnostic("task_ok")
        self.assertNotIn("ERROR", text)
        self.assertIn("MISSION", text)
        self.assertIn("LAST EVENTS", text)

    def test_steps_carry_their_state_glyph(self):
        self.run_mission("task_ko", "Mission", fail=True)
        text = self.mc.diagnostic("task_ko")
        self.assertRegex(text, r"[✓×●○] 01")

    def test_diagnostic_works_after_a_restart(self):
        self.run_mission("task_ko", "Mission", fail=True)
        restarted = MissionControl(FakeBus(), self.db)
        self.assertIn("ÉCHEC", restarted.diagnostic("task_ko"))

    def test_diagnostic_of_an_unknown_mission_is_empty(self):
        self.assertEqual(self.mc.diagnostic("task_inconnue"), "")


# ---------------------------------------------------------------------- health
class FakeLLM:
    def __init__(self, providers):
        self._providers = providers

    def status(self, *a, **k):
        return self._providers


class FakeCore:
    def __init__(self, db, bus, llm=None, tasks=None):
        self.started_at = time.time() - 3661
        self.db = db
        self.events = bus
        self.llm = llm or FakeLLM([])
        self.tasks = tasks or self

    def stats(self):
        return {"active": 1, "total": 7}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.bus = FakeBus()

    def states(self, report):
        return {c["name"]: c["state"] for c in report["components"]}

    def test_every_required_component_is_reported(self):
        report = health_report(FakeCore(self.db, self.bus))
        for name in ("JARVIS Core", "LLM", "Event Bus", "Task Engine", "Database"):
            self.assertIn(name, self.states(report))

    def test_probeable_components_are_online(self):
        core = FakeCore(self.db, self.bus)
        core.events.subscriber_count = lambda: 2
        states = self.states(health_report(core))
        self.assertEqual(states["JARVIS Core"], ONLINE)
        self.assertEqual(states["Database"], ONLINE)
        self.assertEqual(states["Event Bus"], ONLINE)
        self.assertEqual(states["Task Engine"], ONLINE)

    def test_llm_is_online_only_when_really_connected(self):
        core = FakeCore(self.db, self.bus, llm=FakeLLM(
            [{"id": "c1", "type": "openai", "name": "OpenAI", "connected": True}]))
        self.assertEqual(self.states(health_report(core))["LLM"], ONLINE)

    def test_llm_being_probed_is_unknown_not_online(self):
        """« Vérification… » ne doit JAMAIS s'afficher en vert."""
        core = FakeCore(self.db, self.bus, llm=FakeLLM(
            [{"id": "c1", "type": "openai", "name": "OpenAI", "connected": False,
              "detail": "Vérification…"}]))
        self.assertEqual(self.states(health_report(core))["LLM"], UNKNOWN)

    def test_llm_without_provider_is_offline(self):
        core = FakeCore(self.db, self.bus, llm=FakeLLM([]))
        self.assertEqual(self.states(health_report(core))["LLM"], OFFLINE)

    def test_a_broken_probe_never_claims_online(self):
        class Broken(FakeLLM):
            def status(self, *a, **k):
                raise RuntimeError("réseau coupé")

        core = FakeCore(self.db, self.bus, llm=Broken([]))
        report = health_report(core)
        self.assertEqual(self.states(report)["LLM"], UNKNOWN)
        self.assertEqual(report["overall"], DEGRADED)

    def test_a_dead_database_is_offline(self):
        core = FakeCore(self.db, self.bus)

        class DeadDb:
            def scalar(self, *a, **k):
                raise RuntimeError("base verrouillée")

        core.db = DeadDb()
        report = health_report(core)
        self.assertEqual(self.states(report)["Database"], OFFLINE)
        self.assertEqual(report["overall"], OFFLINE)

    def test_states_stay_inside_the_published_vocabulary(self):
        report = health_report(FakeCore(self.db, self.bus))
        for component in report["components"]:
            self.assertIn(component["state"], (ONLINE, OFFLINE, DEGRADED, UNKNOWN))


# ---------------------------------------------------------------- reconnexion
class ReconnectionTests(Base):
    """Le direct peut mourir ; la vérité, elle, reste lisible.

    Le bus LARGUE un abonné SSE trop lent (file pleine) sans fermer la
    connexion HTTP : le navigateur ne voit aucune erreur et se croit connecté.
    C'est pourquoi le panneau re-interroge périodiquement le backend — et
    c'est ce que ces tests verrouillent : après des événements manqués, une
    simple relecture doit redonner l'état exact.
    """

    def test_the_bus_really_drops_a_slow_subscriber(self):
        from jarvis.events import EventBus
        bus = EventBus(self.db)
        sid, queue_ = bus.subscribe()
        self.assertEqual(bus.subscriber_count(), 1)
        for i in range(700):          # file bornée à 500
            bus.emit("system.info", {"i": i})
        self.assertEqual(bus.subscriber_count(), 0,
                         "hypothèse du garde-fou : un client lent est bien largué")

    def test_a_snapshot_after_missed_events_tells_the_truth(self):
        tid = self.run_mission("task_1", "Mission", fail=True)
        # Le client n'a rien reçu de tout cela : il relit, il obtient l'exact.
        snapshot = self.mc.snapshot()
        self.assertEqual(snapshot["recent"][0]["id"], tid)
        self.assertEqual(snapshot["recent"][0]["state"], "FAILED")
        self.assertEqual(self.mc.detail(tid)["error"], "Connexion SSH refusée.")

    def test_history_after_a_reconnect_shows_the_final_state(self):
        tid = "task_live"
        self.bus.emit("task.created", {"id": tid, "name": "Longue", "agent": "jarvis"})
        self.bus.emit("task.started", {"id": tid, "name": "Longue", "agent": "jarvis"})
        self.assertEqual(self.mc.history("RUNNING")["missions"][0]["id"], tid)
        # Rafale pendant laquelle le client est deconnecte, puis fin.
        for i in range(400):
            self.bus.emit("task.progress", {"id": tid, "log": {"level": "info", "message": str(i)}})
        self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        self.assertEqual(self.mc.history("RUNNING")["missions"], [])
        done = self.mc.history("COMPLETED")["missions"]
        self.assertEqual(done[0]["id"], tid)
        self.assertEqual(done[0]["state"], "COMPLETED")

    def test_a_mission_control_rebuilt_from_scratch_reads_the_same_truth(self):
        """Exactement ce que fait un rechargement de page côté navigateur."""
        self.run_mission("task_1", "Mission")
        rebuilt = MissionControl(FakeBus(), self.db)
        self.assertEqual(rebuilt.history("COMPLETED")["missions"][0]["id"], "task_1")
        self.assertTrue(rebuilt.timeline("task_1"))


# ------------------------------------------------------------------ robustesse
class ResilienceTests(unittest.TestCase):
    def test_mission_control_works_without_any_database(self):
        """Sans base, le direct doit continuer : on perd l'historique, pas le suivi."""
        bus = FakeBus()
        mc = MissionControl(bus)
        bus.emit("task.created", {"id": "t1", "name": "Sans base", "agent": "jarvis"})
        bus.emit("task.started", {"id": "t1", "name": "Sans base", "agent": "jarvis"})
        self.assertEqual(mc.snapshot()["current"]["state"], "RUNNING")
        self.assertEqual(mc.history("RUNNING")["missions"][0]["name"], "Sans base")
        bus.emit("task.completed", {"id": "t1", "result": "ok"})
        self.assertEqual(mc.history("COMPLETED")["missions"][0]["name"], "Sans base")

    def test_a_failing_store_never_breaks_the_live_tracking(self):
        class ExplodingStore:
            def save(self, *a, **k):
                raise RuntimeError("disque plein")

            def append_events(self, *a, **k):
                raise RuntimeError("disque plein")

            def list(self, *a, **k):
                raise RuntimeError("disque plein")

            def counts(self):
                return {}

            def detail(self, *a, **k):
                return None

            def timeline(self, *a, **k):
                return []

        bus = FakeBus()
        mc = MissionControl(bus)
        mc.store = ExplodingStore()
        bus.emit("task.created", {"id": "t1", "name": "Malgré tout", "agent": "jarvis"})
        bus.emit("task.started", {"id": "t1", "name": "Malgré tout", "agent": "jarvis"})
        bus.emit("task.completed", {"id": "t1", "result": "ok"})
        self.assertEqual(mc.detail("t1")["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
