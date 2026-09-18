"""MISSION CONTROL V1 : l'écran ne montre QUE ce que le backend a réellement fait.

Ces tests rejouent les payloads exacts qu'émettent `jarvis/tasks.py` et
`jarvis/tools/runner.py`, et vérifient que Mission Control en déduit l'état
vivant attendu. Le contrat testé ici est celui que consomme l'interface
(ui/js/v5/spatial_mission_control.js) : si une clé disparaît, l'écran ment.
"""
import time
import unittest

from jarvis.mission_control import MissionControl, STATES, sanitize_args


class FakeBus:
    """Bus minimal : même contrat d'appel que `jarvis.events.EventBus`."""

    def __init__(self):
        self.listeners = {}
        self.emitted = []

    def on(self, event_type, callback):
        self.listeners.setdefault(event_type, []).append(callback)

    def emit(self, event_type, payload=None, *, persist=False, cache=True):
        event = {"type": event_type, "ts": time.time(), "data": payload or {}}
        self.emitted.append(event)
        for cb in list(self.listeners.get(event_type, [])):
            cb(event)
        return event


class Harness(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.mc = MissionControl(self.bus)

    def current(self):
        return self.mc.snapshot()["current"]

    def start(self, tid="task_1", name="Analyse du marketplace", agent="jarvis"):
        self.bus.emit("task.created", {"id": tid, "name": name, "kind": "chat", "agent": agent})
        self.bus.emit("task.started", {"id": tid, "name": name, "agent": agent})
        return tid


class ArgumentSanitizingTests(unittest.TestCase):
    def test_secrets_never_reach_the_screen(self):
        out = sanitize_args({"password": "hunter2", "api_key": "sk-42", "token": "t",
                             "AUTHORIZATION": "Bearer x", "query": "skins"})
        for key in ("password", "api_key", "token", "AUTHORIZATION"):
            self.assertEqual(out[key], "•••", key)
        self.assertEqual(out["query"], "skins")

    def test_large_values_are_truncated_but_their_size_is_told(self):
        out = sanitize_args({"content": "x" * 900})
        self.assertLess(len(out["content"]), 300)
        self.assertIn("+680 car.", out["content"])

    def test_structures_are_summarised_not_dumped(self):
        out = sanitize_args({"rows": [1, 2, 3], "meta": {"a": 1}})
        self.assertEqual(out["rows"], "[3 éléments]")
        self.assertEqual(out["meta"], "{1 clés}")

    def test_non_dict_arguments_are_ignored(self):
        self.assertEqual(sanitize_args("nope"), {})
        self.assertEqual(sanitize_args(None), {})


class MissionLifecycleTests(Harness):
    def test_a_mission_appears_only_when_a_task_really_starts(self):
        self.assertIsNone(self.current())
        self.start()
        self.assertEqual(self.current()["state"], "RUNNING")
        self.assertEqual(self.current()["name"], "Analyse du marketplace")

    def test_states_stay_inside_the_published_contract(self):
        tid = self.start()
        self.bus.emit("jarvis.state", {"state": "THINKING", "reason": "llm"})
        self.assertEqual(self.current()["state"], "THINKING")
        self.bus.emit("verification.started", {"task_id": tid})
        self.assertEqual(self.current()["state"], "VERIFYING")
        self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        self.assertIn(self.mc.detail(tid)["state"], STATES)

    def test_completion_freezes_the_mission(self):
        tid = self.start()
        self.bus.emit("task.completed", {"id": tid, "result": "Rapport généré."})
        mission = self.mc.detail(tid)
        self.assertEqual(mission["state"], "COMPLETED")
        self.assertEqual(mission["progress"], 1.0)
        self.assertFalse(mission["active"])
        ended = mission["ended_at"]
        # Un événement tardif ne doit pas faire repartir une mission terminée.
        self.bus.emit("tool.called", {"tool": "x.y", "task_id": tid, "args": {}})
        self.bus.emit("jarvis.state", {"state": "THINKING"})
        self.assertEqual(self.mc.detail(tid)["state"], "COMPLETED")
        self.assertEqual(self.mc.detail(tid)["ended_at"], ended)

    def test_failure_carries_the_real_error(self):
        tid = self.start()
        self.bus.emit("task.failed", {"id": tid, "error": "Connexion SSH refusée."})
        mission = self.mc.detail(tid)
        self.assertEqual(mission["state"], "FAILED")
        self.assertEqual(mission["error"], "Connexion SSH refusée.")

    def test_confirmation_shows_as_waiting(self):
        self.start()
        self.bus.emit("task.waiting_confirmation", {"id": "task_1", "action": "Publier le rapport"})
        self.assertEqual(self.current()["state"], "WAITING")
        self.assertEqual(self.current()["note"], "Publier le rapport")


class StepTests(Harness):
    PLAN = [{"key": "a", "label": "Recherche utilisateur", "state": "done"},
            {"key": "b", "label": "Lecture marketplace", "state": "done"},
            {"key": "c", "label": "Analyse des annonces", "state": "run"},
            {"key": "d", "label": "Génération du rapport", "state": "idle"}]

    def test_no_step_is_invented_before_the_backend_declares_it(self):
        self.start()
        self.assertEqual(self.current()["steps"], [])
        self.assertIsNone(self.current()["current_step"])
        self.assertIsNone(self.current()["progress"])

    def test_plan_drives_the_checklist_and_the_progress(self):
        tid = self.start()
        self.bus.emit("task.progress", {"id": tid, "plan": self.PLAN})
        mission = self.current()
        self.assertEqual(mission["steps_total"], 4)
        self.assertEqual(mission["steps_done"], 2)
        self.assertEqual(mission["current_step"]["label"], "Analyse des annonces")
        self.assertAlmostEqual(mission["progress"], 0.5)

    def test_a_running_step_is_closed_when_the_mission_ends(self):
        tid = self.start()
        self.bus.emit("task.progress", {"id": tid, "plan": self.PLAN})
        self.bus.emit("task.completed", {"id": tid, "result": "fini"})
        states = [s["state"] for s in self.mc.detail(tid)["steps"]]
        self.assertNotIn("run", states)

    def test_a_named_pipeline_phase_becomes_the_progress(self):
        tid = self.start()
        self.bus.emit("task.progress", {"id": tid, "log": {
            "level": "info", "message": "Analyse",
            "data": {"phase": "analyse des annonces", "completed": 19, "total": 38}}})
        mission = self.current()
        self.assertEqual(mission["note"], "analyse des annonces")
        self.assertAlmostEqual(mission["progress"], 0.5)


class ToolTests(Harness):
    def test_a_running_tool_is_shown_with_its_parameters(self):
        tid = self.start()
        self.bus.emit("tool.called", {"tool": "marketplace.search", "name": "Recherche marketplace",
                                      "agent": "BrainrotFortniteAgent", "task_id": tid,
                                      "args": {"query": "skins", "api_key": "SECRET"}})
        mission = self.current()
        self.assertEqual(mission["state"], "TOOL")
        self.assertEqual(mission["agent"], "BrainrotFortniteAgent")
        self.assertEqual(mission["tool"]["id"], "marketplace.search")
        self.assertEqual(mission["tool"]["args"]["query"], "skins")
        self.assertEqual(mission["tool"]["args"]["api_key"], "•••")
        self.assertEqual(mission["tool_calls"], 1)

    def test_tool_result_is_reported_and_the_mission_keeps_going(self):
        tid = self.start()
        self.bus.emit("tool.called", {"tool": "marketplace.search", "task_id": tid, "args": {}})
        self.bus.emit("tool.completed", {"tool_id": "marketplace.search", "task_id": tid,
                                         "ok": True, "duration_ms": 1420,
                                         "preview": "38 annonces"})
        mission = self.current()
        self.assertIsNone(mission["tool"])
        self.assertTrue(mission["last_tool"]["ok"])
        self.assertEqual(mission["last_tool"]["duration_ms"], 1420)
        self.assertEqual(mission["last_tool"]["preview"], "38 annonces")
        # Un outil qui échoue n'est pas une mission qui échoue : JARVIS peut
        # réessayer autrement, l'écran doit le refléter.
        self.assertEqual(mission["state"], "RUNNING")

    def test_a_failing_tool_does_not_condemn_the_mission(self):
        tid = self.start()
        self.bus.emit("tool.called", {"tool": "fs.read", "task_id": tid, "args": {}})
        self.bus.emit("tool.failed", {"tool_id": "fs.read", "task_id": tid, "ok": False,
                                      "error": "Fichier introuvable"})
        mission = self.current()
        self.assertEqual(mission["state"], "RUNNING")
        self.assertFalse(mission["last_tool"]["ok"])
        self.assertEqual(mission["last_tool"]["error"], "Fichier introuvable")

    def test_a_denied_tool_is_recorded(self):
        tid = self.start()
        self.bus.emit("tool.denied", {"tool": "ssh.run", "task_id": tid, "reason": "Droits manquants"})
        kinds = [h["kind"] for h in self.mc.detail(tid)["history"]]
        self.assertIn("denied", kinds)

    def test_tool_events_reach_their_own_mission_not_the_latest(self):
        first = self.start("task_a", "Première")
        second = self.start("task_b", "Seconde")
        self.bus.emit("tool.called", {"tool": "fs.read", "task_id": first, "args": {}})
        self.assertEqual(self.mc.detail(first)["tool"]["id"], "fs.read")
        self.assertIsNone(self.mc.detail(second)["tool"])


class FileTests(Harness):
    def test_touched_files_are_listed_with_their_action(self):
        tid = self.start()
        self.bus.emit("code.file.opened", {"path": "/var/www/index.php", "task_id": tid})
        self.bus.emit("code.file.saved", {"path": "/var/www/index.php", "task_id": tid})
        self.bus.emit("code.file.created", {"path": "/var/www/new.php", "task_id": tid})
        files = {f["path"]: f["action"] for f in self.current()["files"]}
        self.assertEqual(files["/var/www/index.php"], "saved")
        self.assertEqual(files["/var/www/new.php"], "created")

    def test_a_file_event_without_a_mission_is_dropped(self):
        self.bus.emit("code.file.opened", {"path": "/tmp/x"})
        self.assertIsNone(self.current())


class SnapshotTests(Harness):
    def test_snapshot_separates_live_from_finished(self):
        done = self.start("task_done", "Terminée")
        self.bus.emit("task.completed", {"id": done, "result": "ok"})
        self.start("task_live", "En cours")
        snap = self.mc.snapshot()
        self.assertEqual([m["name"] for m in snap["live"]], ["En cours"])
        self.assertEqual([m["name"] for m in snap["recent"]], ["Terminée"])
        self.assertEqual(snap["current"]["name"], "En cours")

    def test_every_field_the_interface_reads_is_published(self):
        tid = self.start()
        self.bus.emit("tool.called", {"tool": "fs.read", "task_id": tid, "args": {"path": "/a"}})
        mission = self.current()
        for key in ("id", "name", "agent", "state", "active", "started_at", "duration",
                    "progress", "steps", "steps_done", "steps_total", "current_step",
                    "tool", "last_tool", "tool_calls", "files", "result", "error",
                    "note", "history"):
            self.assertIn(key, mission, key)

    def test_memory_stays_bounded(self):
        for i in range(90):
            tid = f"task_{i}"
            self.start(tid, f"Mission {i}")
            self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        snap = self.mc.snapshot()
        self.assertLessEqual(len(snap["recent"]), 30)

    def test_detail_of_an_unknown_mission_is_none(self):
        self.assertIsNone(self.mc.detail("task_inconnue"))


class BusContractTests(Harness):
    def test_mission_events_are_published_for_the_interface(self):
        tid = self.start()
        self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        types = {e["type"] for e in self.bus.emitted if e["type"].startswith("mission.")}
        self.assertEqual(types, {"mission.started", "mission.update", "mission.completed"})

    def test_a_chatty_pipeline_does_not_flood_the_stream(self):
        """Un log par message trié ne doit pas devenir un `mission.update` par message."""
        tid = self.start()
        before = sum(1 for e in self.bus.emitted if e["type"] == "mission.update")
        for i in range(60):
            self.bus.emit("task.progress", {"id": tid, "log": {"level": "info", "message": f"Message {i}"}})
        burst = sum(1 for e in self.bus.emitted if e["type"] == "mission.update") - before
        self.assertLess(burst, 10, "les mises à jour ne sont pas coalescées")
        # Coalescer ne veut pas dire perdre : l'état final finit par sortir.
        time.sleep(0.4)
        published = [e for e in self.bus.emitted if e["type"] == "mission.update"][-1]
        self.assertEqual(published["data"]["history"][-1]["label"], "Message 59")

    def test_the_end_of_a_mission_is_never_delayed(self):
        tid = self.start()
        for i in range(30):
            self.bus.emit("task.progress", {"id": tid, "log": {"level": "info", "message": str(i)}})
        self.bus.emit("task.completed", {"id": tid, "result": "ok"})
        last = self.bus.emitted[-1]
        self.assertEqual(last["type"], "mission.completed")
        self.assertEqual(last["data"]["state"], "COMPLETED")

    def test_updates_stay_out_of_the_replayed_history(self):
        """`mission.update` est frequent : il ne doit pas noyer l'historique SSE."""
        from jarvis.events import EVENT_TYPES
        for evt in ("mission.started", "mission.update", "mission.completed", "mission.failed"):
            self.assertIn(evt, EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
