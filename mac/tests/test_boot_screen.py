"""Tests du Boot Screen — mocks uniquement, aucune fenêtre, aucun processus.

Couvre la checklist §16 : HEALTHY, DEGRADED, BLOCKED, LLM hors ligne, repli LLM
actif, JARVIS déjà lancé, conflit de port, démarrage réussi/échoué, ouverture du
diagnostic et ouverture automatique de l'interface.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import boot  # noqa: E402
from jarvis import doctor  # noqa: E402

CHECK_NAMES = ("python", "repository", "database", "eventbus", "llm", "tool_registry",
               "core", "api_server", "mission_control", "brainrot_imports",
               "comfyui", "supervisor")


def _check(name, status=doctor.OK, *, severity=doctor.BLOCKING, blocking=False,
           providers=None, total=None):
    details = {}
    if providers is not None:
        details["providers"] = providers
    if total is not None:
        details["total"] = total
    return {"name": name, "status": status, "severity": severity,
            "required": severity == doctor.BLOCKING, "blocking": bool(blocking),
            "message": "", "details": details}


def _report(health=doctor.HEALTHY, *, checks=None, blocking=None, degraded=None):
    return {"health": health, "blocking": blocking or [], "degraded": degraded or [],
            "checks": checks if checks is not None else [], "warnings": 0, "errors": 0}


def _full_checks(status=doctor.OK, severity=doctor.BLOCKING):
    return [_check(name, status, severity=severity,
                   total=165 if name == "tool_registry" else None)
            for name in CHECK_NAMES]


def _status(agents_total=4, llms=2):
    return {"core": {"agents": {"total": agents_total, "running": 0,
                                "status": "standby"},
                     "llms": {"count": llms, "status": "connected"}}}


class FakeView:
    """Vue sans Tk : reçoit les décisions du contrôleur de façon synchrone."""

    def __init__(self):
        self.controller = None
        self.renders = 0
        self.outcomes = []
        self.failures = 0
        self.scheduled = []
        self.closed = 0
        self.posted = []

    def post(self, event):
        self.posted.append(event)
        if self.controller is not None:
            self.controller.handle_event(event)

    def render(self, model):
        self.renders += 1

    def show_outcome(self, outcome, model):
        self.outcomes.append(outcome)

    def show_failure(self, model):
        self.failures += 1

    def schedule_close(self, delay):
        self.scheduled.append(delay)

    def close(self):
        self.closed += 1


class ModelDiagnosisTests(unittest.TestCase):
    def test_health_sain_tous_prets(self):
        model = boot.BootModel()
        model.apply({"type": "diagnosis", "report": _report(checks=_full_checks())})
        self.assertEqual(model.ready_count(), 12)
        self.assertEqual(model.components["agents"].status, boot.WAITING)

    def test_warning_devient_degrade(self):
        model = boot.BootModel()
        checks = _full_checks()
        checks[-1] = _check("supervisor", doctor.WARNING, severity=doctor.DEGRADED)
        model.apply({"type": "diagnosis", "report": _report(checks=checks)})
        self.assertEqual(model.components["supervisor"].status, boot.DEGRADED_ST)

    def test_erreur_bloquante_devient_failed(self):
        model = boot.BootModel()
        checks = _full_checks()
        checks[0] = _check("python", doctor.ERROR, blocking=True)
        model.apply({"type": "diagnosis",
                     "report": _report(doctor.BLOCKED, checks=checks, blocking=["python"])})
        self.assertEqual(model.components["python"].status, boot.FAILED)
        self.assertEqual(model.blocking, ["python"])

    def test_controle_optionnel_devient_skipped(self):
        model = boot.BootModel()
        checks = _full_checks()
        checks[-2] = _check("comfyui", doctor.OPTIONAL, severity=doctor.OPTIONAL)
        model.apply({"type": "diagnosis", "report": _report(checks=checks)})
        self.assertEqual(model.components["comfyui"].status, boot.SKIPPED)

    def test_llm_repli_reste_pret(self):
        model = boot.BootModel()
        model.apply({"type": "llm", "state": "fallback", "message": "",
                     "providers": ["openai", "anthropic"]})
        self.assertEqual(model.components["llm"].status, boot.READY)
        self.assertIn("openai", model.components["llm"].detail)

    def test_llm_hors_ligne_bloquant_devient_failed(self):
        model = boot.BootModel()
        model.health = doctor.BLOCKED
        model.apply({"type": "llm", "state": "offline", "message": "", "providers": []})
        self.assertEqual(model.components["llm"].status, boot.FAILED)

    def test_llm_launchable_devient_starting(self):
        model = boot.BootModel()
        model.apply({"type": "llm", "state": "launchable", "message": "", "providers": []})
        self.assertEqual(model.components["llm"].status, boot.STARTING)

    def test_resultat_ready_prepare_la_fin(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "ready", "version": "3.0.0"})
        self.assertTrue(model.finished)
        self.assertEqual(model.banner(), ("JARVIS READY", "ok"))
        self.assertEqual(model.version, "3.0.0")

    def test_resultat_degrade_banniere_avertissement(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "degraded", "degraded": ["comfyui"]})
        self.assertTrue(model.finished)
        self.assertEqual(model.banner()[1], "warn")

    def test_resultat_already_running(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "already_running", "version": "3"})
        self.assertTrue(model.finished)
        self.assertEqual(model.banner(), ("JARVIS ALREADY ONLINE", "ok"))

    def test_resultat_bloque_ne_finit_pas(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "blocked", "blocking": ["api_server"]})
        self.assertFalse(model.finished)
        self.assertEqual(model.banner(), ("JARVIS START FAILED", "err"))

    def test_status_enrichit_agents_et_llm(self):
        model = boot.BootModel()
        model.apply({"type": "status", "status": _status(agents_total=5, llms=2)})
        self.assertEqual(model.components["agents"].status, boot.READY)
        self.assertEqual(model.components["agents"].detail, "5")
        self.assertEqual(model.components["llm"].status, boot.READY)

    def test_services_degrades_excluent_les_critiques(self):
        model = boot.BootModel()
        checks = _full_checks()
        checks[0] = _check("python", doctor.ERROR, blocking=True)
        checks[-1] = _check("supervisor", doctor.WARNING, severity=doctor.DEGRADED)
        model.apply({"type": "diagnosis", "report": _report(checks=checks)})
        keys = {c.key for c in model.degraded_services()}
        self.assertIn("supervisor", keys)
        self.assertNotIn("python", keys)


class ControllerDecisionTests(unittest.TestCase):
    def _controller(self, view, **kwargs):
        kwargs.setdefault("open_interface_fn", lambda: True)
        kwargs.setdefault("open_doctor_fn", lambda: True)
        kwargs.setdefault("status_probe", lambda: None)
        controller = boot.BootController(view, **kwargs)
        view.controller = controller
        return controller

    def test_ready_ouvre_interface_et_ferme(self):
        view = FakeView()
        controller = self._controller(view)
        controller.handle_event({"type": "result", "outcome": "ready"})
        self.assertTrue(controller.interface_opened)
        self.assertEqual(view.outcomes, ["ready"])
        self.assertEqual(view.scheduled, [controller.close_delay_s])

    def test_degraded_ouvre_interface(self):
        view = FakeView()
        controller = self._controller(view)
        controller.handle_event({"type": "result", "outcome": "degraded"})
        self.assertTrue(controller.interface_opened)
        self.assertEqual(view.scheduled, [controller.close_delay_s])

    def test_already_running_ouvre_une_fois(self):
        view = FakeView()
        opens = []
        controller = self._controller(view, open_interface_fn=lambda: opens.append(1) or True)
        controller.handle_event({"type": "result", "outcome": "already_running"})
        self.assertEqual(len(opens), 1)

    def test_blocked_garde_la_fenetre_et_ne_lance_pas_interface(self):
        view = FakeView()
        opens = []
        controller = self._controller(view, open_interface_fn=lambda: opens.append(1) or True)
        controller.handle_event({"type": "result", "outcome": "blocked",
                                 "blocking": ["api_server"], "message": "boom"})
        self.assertEqual(view.failures, 1)
        self.assertEqual(opens, [])
        self.assertEqual(view.scheduled, [])

    def test_failed_garde_la_fenetre(self):
        view = FakeView()
        controller = self._controller(view)
        controller.handle_event({"type": "result", "outcome": "failed",
                                 "message": "démarrage impossible"})
        self.assertEqual(view.failures, 1)
        self.assertFalse(controller.finished)

    def test_resultat_duplique_n_ouvre_qu_une_fois(self):
        view = FakeView()
        opens = []
        controller = self._controller(view, open_interface_fn=lambda: opens.append(1) or True)
        controller.handle_event({"type": "result", "outcome": "ready"})
        controller.handle_event({"type": "result", "outcome": "ready"})
        self.assertEqual(len(opens), 1)
        self.assertEqual(view.scheduled, [controller.close_delay_s])

    def test_diagnostic_appelle_le_launcher(self):
        view = FakeView()
        calls = []
        controller = self._controller(view, open_doctor_fn=lambda: calls.append(1) or True)
        self.assertTrue(controller.request_doctor())
        self.assertEqual(calls, [1])
        self.assertTrue(controller.diagnostic_opened)

    def test_request_close_ferme_la_vue(self):
        view = FakeView()
        controller = self._controller(view)
        controller.request_close()
        self.assertEqual(view.closed, 1)

    def test_worker_execute_le_starter_et_sonde_le_status(self):
        view = FakeView()
        probes = []

        def starter(publish):
            publish({"type": "diagnosis", "report": _report(checks=_full_checks())})
            publish({"type": "result", "outcome": "ready", "version": "3.0.0"})
            return 0

        def status_probe():
            probes.append(1)
            return _status(agents_total=7)

        controller = self._controller(view, starter=starter, status_probe=status_probe)
        thread = controller.start()
        thread.join(timeout=5)
        self.assertTrue(controller.finished)
        self.assertEqual(view.outcomes, ["ready"])
        self.assertEqual(probes, [1])
        self.assertEqual(controller.model.components["agents"].status, boot.READY)
        self.assertEqual(controller.model.components["agents"].detail, "7")

    def test_worker_exception_affiche_un_echec(self):
        view = FakeView()

        def starter(publish):
            raise RuntimeError("boum")

        controller = self._controller(view, starter=starter)
        controller.start().join(timeout=5)
        self.assertEqual(view.failures, 1)
        self.assertIn("boum", controller.model.message)


class ProgressAndActivityTests(unittest.TestCase):
    def _checks_without(self, *excluded):
        return [_check(name) for name in CHECK_NAMES if name not in excluded]

    def test_pourcentage_reel(self):
        model = boot.BootModel()
        self.assertEqual(model.progress_percent(), 0)
        model.apply({"type": "diagnosis", "report": _report(checks=_full_checks())})
        self.assertEqual(model.ready_count(), 12)
        self.assertEqual(model.progress_percent(), round(100 * 12 / 13))
        model.apply({"type": "status", "status": _status(agents_total=5)})
        self.assertEqual(model.progress_percent(), 100)

    def test_phases_reelles(self):
        model = boot.BootModel()
        self.assertEqual(model.progress_phase(), boot.PHASE_INIT)
        model.apply({"type": "phase", "phase": "preflight"})
        self.assertEqual(model.progress_phase(), boot.PHASE_ENV)
        model.apply({"type": "diagnosis",
                     "report": _report(checks=self._checks_without("mission_control"))})
        self.assertEqual(model.progress_phase(), boot.PHASE_TOOLS)
        model.apply({"type": "phase", "phase": "llm_start"})
        self.assertEqual(model.progress_phase(), boot.PHASE_LLM)
        model.apply({"type": "phase", "phase": "launch"})
        self.assertEqual(model.progress_phase(), boot.PHASE_AGENTS)
        model.apply({"type": "status", "status": _status(agents_total=7)})
        self.assertEqual(model.progress_phase(), boot.PHASE_MISSION)
        model.apply({"type": "diagnosis",
                     "report": _report(checks=self._checks_without())})
        self.assertEqual(model.progress_phase(), boot.PHASE_API)
        model.apply({"type": "result", "outcome": "ready"})
        self.assertEqual(model.progress_phase(), boot.PHASE_READY)

    def test_phase_abandon_si_bloque(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "blocked", "blocking": ["api_server"]})
        self.assertEqual(model.progress_phase(), boot.PHASE_ABORT)

    def test_activite_alimentee_par_les_evenements_reels(self):
        model = boot.BootModel()
        model.apply({"type": "phase", "phase": "preflight"})
        model.apply({"type": "diagnosis", "report": _report(checks=_full_checks())})
        model.apply({"type": "llm", "state": "running", "message": "", "providers": []})
        model.apply({"type": "ports", "states": {"8080": "listening", "8765": "free"}})
        model.apply({"type": "process", "pid": 99})
        model.apply({"type": "status", "status": _status(agents_total=7)})
        model.apply({"type": "result", "outcome": "ready"})
        joined = "\n".join(model.activity)
        self.assertIn("PHASE · VALIDATION DE L'ENVIRONNEMENT", joined)
        self.assertIn("PYTHON READY", joined)
        self.assertIn("TOOLS READY (165)", joined)
        self.assertIn("LLM SERVER ALREADY RUNNING (127.0.0.1:8080)", joined)
        self.assertIn("PORT 8080 LISTENING", joined)
        self.assertIn("JARVIS PROCESS STARTED (PID 99)", joined)
        self.assertIn("AGENTS READY · 7", joined)
        self.assertIn("SYSTÈME PRÊT — OUVERTURE DE L'INTERFACE", joined)
        self.assertNotIn("PORT 8765 LISTENING", joined)

    def test_activite_ne_duplique_pas_les_repetitions(self):
        model = boot.BootModel()
        model.apply({"type": "llm", "state": "running", "message": "", "providers": []})
        count = len(model.activity)
        model.apply({"type": "llm", "state": "running", "message": "", "providers": []})
        self.assertEqual(len(model.activity), count)

    def test_banniere_degrade_liste_les_services(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "degraded", "degraded": ["comfyui", "supervisor"]})
        self.assertEqual(model.banner(), ("JARVIS READY", "warn"))
        self.assertIn("COMFYUI", model.banner_sub())
        self.assertIn("SUPERVISOR", model.banner_sub())

    def test_composant_failed_utilise_le_bloquant(self):
        model = boot.BootModel()
        model.apply({"type": "result", "outcome": "blocked", "blocking": ["repository"]})
        self.assertEqual(model.failed_component(), "REPOSITORY")

    def test_starting_resolu_par_le_resultat(self):
        model = boot.BootModel()
        model.apply({"type": "llm", "state": "launchable", "message": "", "providers": []})
        self.assertEqual(model.components["llm"].status, boot.STARTING)
        model.apply({"type": "result", "outcome": "ready"})
        self.assertEqual(model.components["llm"].status, boot.READY)

    def test_close_delay_laisse_la_transition_se_voir(self):
        view = FakeView()
        controller = boot.BootController(view, status_probe=lambda: None)
        self.assertGreaterEqual(controller.close_delay_s, 1.5)


class MiscTests(unittest.TestCase):
    def test_animations_desactivees_par_env(self):
        with mock.patch.dict("os.environ", {"JARVIS_NO_ANIM": "1"}):
            self.assertFalse(boot.animations_enabled())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(boot.animations_enabled())

    def test_animations_desactivees_par_boot_animations(self):
        with mock.patch.dict("os.environ", {"JARVIS_BOOT_ANIMATIONS": "0"}, clear=True):
            self.assertFalse(boot.animations_enabled())
        with mock.patch.dict("os.environ", {"JARVIS_BOOT_ANIMATIONS": "1"}, clear=True):
            self.assertTrue(boot.animations_enabled())

    def test_composants_uniques_et_attendus(self):
        keys = [key for key, _l, _c, _crit in boot.COMPONENTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("llm", keys)
        self.assertIn("agents", keys)
        checks = {check for _k, _l, check, _crit in boot.COMPONENTS if check}
        self.assertIn("brainrot_imports", checks)

    def test_probe_status_renvoie_none_si_serveur_absent(self):
        with mock.patch.object(boot, "_http_json", side_effect=OSError("refusé")):
            self.assertIsNone(boot.probe_status())

    def test_probe_status_renvoie_le_dict(self):
        with mock.patch.object(boot, "_http_json", return_value={"core": {"agents": {}}}):
            self.assertEqual(boot.probe_status(), {"core": {"agents": {}}})


if __name__ == "__main__":
    unittest.main()
