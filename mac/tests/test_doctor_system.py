"""Doctor système : chaque diagnostic doit être exact, jamais optimiste.

Ces tests ne touchent AUCUN réseau : les sondes sont simulées par des fakes
dont la forme est exactement celle des vraies sondes (c'est leur contrat).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from jarvis import doctor


class PythonCheckTests(unittest.TestCase):
    def test_check_reussi(self):
        # Un interpréteur conforme (3.10+) doit être validé : la logique de la
        # sonde, pas le Python du poste de test.
        with mock.patch.object(doctor.sys, "version_info", (3, 12, 3)):
            result = doctor._sys_check_python()
        self.assertEqual(result["status"], doctor.OK)
        self.assertEqual(result["blocking"], False)

    def test_python_trop_ancien_est_bloquant(self):
        with mock.patch.object(doctor.sys, "version_info", (3, 9, 0)):
            result = doctor._sys_check_python()
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])
        self.assertTrue(result["recommendation"])


class ImportsCheckTests(unittest.TestCase):
    def test_check_reussi(self):
        probe = {name: {"ok": True} for name in doctor.CRITICAL_IMPORTS}
        result = doctor._sys_check_imports(probe)
        self.assertEqual(result["status"], doctor.OK)

    def test_import_casse_restitue_type_message_fichier(self):
        probe = {name: {"ok": True} for name in doctor.CRITICAL_IMPORTS}
        probe["jarvis.core"] = {"ok": False, "error": "ModuleNotFoundError",
                                "message": "No module named 'tralala'",
                                "file": "jarvis/core.py", "line": 12}
        result = doctor._sys_check_imports(probe)
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])
        for fragment in ("jarvis.core", "ModuleNotFoundError", "tralala",
                         "jarvis/core.py", "12"):
            self.assertIn(fragment, result["detail"])
        # L'exception n'est pas masquée : elle est dans les détails aussi.
        self.assertEqual(result["details"]["broken"]["jarvis.core"]["error"],
                         "ModuleNotFoundError")


class LlmServerTests(unittest.TestCase):
    def test_serveur_llm_offline(self):
        probes = [{"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                   "status": "offline", "models": [], "error": "URLError: refused"}]
        result = doctor._sys_check_llm_server(probes)
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])
        self.assertIn("127.0.0.1:8080", result["recommendation"])

    def test_serveur_llm_online_simule(self):
        probes = [{"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                   "status": "online",
                   "models": ["qwen2.5-coder-14b-act", "qwen3-coder-30b-plan"],
                   "error": ""}]
        server = doctor._sys_check_llm_server(probes)
        model = doctor._sys_check_llm_model(probes)
        self.assertEqual(server["status"], doctor.OK)
        self.assertEqual(model["status"], doctor.OK)
        # Les Model IDs viennent de la RÉPONSE simulée, jamais d'une liste codée.
        self.assertIn("qwen3-coder-30b-plan", model["detail"])

    def test_degraded_sans_modele(self):
        probes = [{"type": "openai", "url": "http://x/v1/models",
                   "status": "degraded", "models": [],
                   "error": "endpoint joignable mais aucun modèle listé"}]
        server = doctor._sys_check_llm_server(probes)
        model = doctor._sys_check_llm_model(probes)
        self.assertEqual(server["status"], doctor.WARNING)
        self.assertEqual(model["status"], doctor.ERROR)

    def test_probe_llm_normalise_les_urls(self):
        with mock.patch.object(doctor, "_http_json") as fake:
            fake.return_value = {"data": [{"id": "modele-a"}]}
            probes = doctor.probe_llm([("openai", "http://127.0.0.1:8080/v1/")])
        self.assertEqual(probes[0]["status"], "online")
        self.assertEqual(probes[0]["models"], ["modele-a"])
        fake.assert_called_once_with("http://127.0.0.1:8080/v1/models")


class SecretsCheckTests(unittest.TestCase):
    def test_les_secrets_sont_masques(self):
        """La valeur d'une clé ne doit JAMAIS apparaître, seulement SET/MISSING."""
        env = {"OPENAI_API_KEY": "sk-supersecret-AZERTY123", "JARVIS_MASTER_KEY": ""}
        result = doctor._sys_check_secrets(env, vault_rows=[
            {"field": "api_key", "n": 2}, {"field": "token", "n": 1}])
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sk-supersecret-AZERTY123", dumped)
        self.assertNotIn("AZERTY123", dumped)
        self.assertIn("OPENAI_API_KEY", dumped)
        self.assertIn("SET", result["detail"])
        self.assertEqual(result["status"], doctor.OK)

    def test_aucun_secret_est_un_warning_non_bloquant(self):
        result = doctor._sys_check_secrets(env={}, vault_rows=[])
        self.assertEqual(result["status"], doctor.WARNING)
        self.assertFalse(result["blocking"])
        self.assertIn("MISSING", result["detail"])


class PortsCheckTests(unittest.TestCase):
    def test_port_occupe_par_jarvis_est_ok(self):
        states = {8080: {"port": 8080, "state": "listening", "pid": 111, "name": "python.exe"},
                  8765: {"port": 8765, "state": "listening", "pid": 222, "name": "python.exe"}}
        ports = doctor._sys_check_ports(states)
        api = doctor._sys_check_api_server(states[8765])
        self.assertEqual(ports["status"], doctor.OK)
        self.assertEqual(api["status"], doctor.OK)
        self.assertIn("pid 222", api["detail"])

    def test_port_api_occupe_par_un_processus_inattendu_est_bloquant(self):
        intruder = {"port": 8765, "state": "listening", "pid": 333, "name": "chrome.exe"}
        states = {8080: {"port": 8080, "state": "free"}, 8765: intruder}
        ports = doctor._sys_check_ports(states)
        api = doctor._sys_check_api_server(intruder)
        self.assertEqual(ports["status"], doctor.ERROR)
        self.assertTrue(ports["blocking"])
        self.assertEqual(api["status"], doctor.ERROR)
        self.assertTrue(api["blocking"])
        self.assertIn("chrome.exe", api["detail"])

    def test_ports_libres(self):
        states = {8080: {"port": 8080, "state": "free"},
                  8765: {"port": 8765, "state": "free"}}
        self.assertEqual(doctor._sys_check_ports(states)["status"], doctor.OK)
        self.assertEqual(doctor._sys_check_api_server(states[8765])["status"], doctor.OK)


class ToolRegistryCheckTests(unittest.TestCase):
    def test_registry_vide(self):
        result = doctor._sys_check_tools({"total": 0, "duplicates": [], "no_handler": [],
                                          "risks": {}, "registered": 0})
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertIn("registry vide", result["detail"])

    def test_tool_duplique(self):
        result = doctor._sys_check_tools({"total": 3, "duplicates": ["fs.read"],
                                          "no_handler": [], "risks": {"read_only": 3},
                                          "registered": 4})
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertIn("DOUBLONS D'ID : fs.read", result["detail"])

    def test_handler_absent(self):
        result = doctor._sys_check_tools({"total": 1, "duplicates": [],
                                          "no_handler": ["outil.casse"],
                                          "risks": {"read_only": 1}, "registered": 1})
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertIn("outil.casse", result["detail"])

    def test_inventaire_normal(self):
        payload = {"total": 12, "duplicates": [], "no_handler": [],
                   "risks": {"read_only": 10, "sensitive": 1, "destructive": 1},
                   "registered": 12}
        result = doctor._sys_check_tools(payload)
        self.assertEqual(result["status"], doctor.OK)
        self.assertIn("TOTAL 12", result["detail"])
        self.assertIn("destructive=1", result["detail"])


class MissionControlCheckTests(unittest.TestCase):
    def test_tout_present(self):
        probe = {"jarvis.mission_control": {"ok": True},
                 "jarvis.mission_store": {"ok": True}}
        result = doctor._sys_check_mission_control(probe, missing_files=[],
                                                   missing_tables=[])
        self.assertEqual(result["status"], doctor.OK)

    def test_import_casse_signale_la_dependance_bloquante(self):
        probe = {"jarvis.mission_control": {"ok": False, "error": "ImportError",
                                             "message": "boom", "file": "x.py", "line": 3},
                 "jarvis.mission_store": {"ok": True}}
        result = doctor._sys_check_mission_control(probe, missing_files=[],
                                                   missing_tables=[])
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertIn("dépendance bloquante", result["detail"])

    def test_tables_absentes_est_un_warning(self):
        probe = {"jarvis.mission_control": {"ok": True},
                 "jarvis.mission_store": {"ok": True}}
        result = doctor._sys_check_mission_control(probe, missing_files=[],
                                                   missing_tables=["missions"])
        self.assertEqual(result["status"], doctor.WARNING)


class BrainrotCheckTests(unittest.TestCase):
    def test_imports_ok(self):
        probe = {name: {"ok": True} for name in doctor.BRAINROT_IMPORTS}
        self.assertEqual(doctor._sys_check_brainrot(probe)["status"], doctor.OK)

    def test_import_casse_est_un_warning_sans_reparation(self):
        probe = {name: {"ok": True} for name in doctor.BRAINROT_IMPORTS}
        probe["jarvis.brainrot_operator"] = {"ok": False, "error": "ModuleNotFoundError",
                                            "message": "No module named 'x'",
                                            "file": "jarvis/brainrot_operator.py", "line": 7}
        result = doctor._sys_check_brainrot(probe)
        self.assertEqual(result["status"], doctor.WARNING)
        self.assertIn("protégé", result["recommendation"])


class DatabaseCheckTests(unittest.TestCase):
    def test_base_absente_est_un_warning_non_bloquant(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = doctor._sys_check_database(os.path.join(tmp, "inconnue.db"))
        self.assertEqual(result["status"], doctor.WARNING)
        self.assertFalse(result["blocking"])

    def test_base_illisible_est_bloquant(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
            fh.write(b"ceci n'est pas une base sqlite")
            path = fh.name
        try:
            result = doctor._sys_check_database(path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])

    def test_base_valide(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.db")
            con = sqlite3.connect(path)
            con.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);"
                "CREATE TABLE tasks(id TEXT);"
                "CREATE TABLE missions(id TEXT);"
                "CREATE TABLE mission_events(id INTEGER);"
                "CREATE TABLE connectors(id TEXT);"
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                "INSERT INTO meta(key, value) VALUES('schema_version', '99');"
                "INSERT INTO settings(key, value, updated_at) VALUES('ai/x', '1', 0);")
            con.commit()
            con.close()
            result = doctor._sys_check_database(path)
        self.assertEqual(result["status"], doctor.OK)
        self.assertIn("v99", result["detail"])


class EventBusCheckTests(unittest.TestCase):
    def test_bus_isole_receptionne_levenement_de_test(self):
        self.assertEqual(doctor._sys_check_eventbus()["status"], doctor.OK)

    def test_bus_casse_est_bloquant(self):
        with mock.patch("jarvis.events.EventBus", side_effect=TypeError("plus de bus")):
            result = doctor._sys_check_eventbus()
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])


class ModeJsonTests(unittest.TestCase):
    def test_rapport_systeme_est_du_json_valide(self):
        """`system_diagnose` sans Core, sondes simulées → JSON strictement valide."""
        imports_ok = {name: {"ok": True} for name in
                      doctor.CRITICAL_IMPORTS + doctor.BRAINROT_IMPORTS}
        with mock.patch.object(doctor.sys, "version_info", (3, 12, 3)), \
             mock.patch.object(doctor, "probe_imports", return_value=imports_ok), \
             mock.patch.object(doctor, "probe_tools", return_value={
                 "total": 5, "duplicates": [], "no_handler": [], "risks": {"read_only": 5},
                 "registered": 5}), \
             mock.patch.object(doctor, "_llm_endpoints", return_value=[
                 ("openai", "http://127.0.0.1:8080/v1")]), \
             mock.patch.object(doctor, "probe_llm", return_value=[
                 {"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                  "status": "online", "models": ["m1"], "error": ""}]), \
             mock.patch.object(doctor, "_port_state", return_value={
                 "port": 8765, "state": "free"}), \
             mock.patch.object(doctor, "_sqlite_ro", side_effect=Exception("no db")), \
             mock.patch.object(doctor, "DB_PATH",
                               os.path.join(tempfile.gettempdir(), "doctor_absent.db")), \
             mock.patch.object(doctor, "_sys_check_core", return_value=(
                 {"name": "core", "label": "Core startup", "status": doctor.OK,
                  "detail": "amorcé", "recommendation": "", "blocking": False,
                  "details": {}}, None)):
            with mock.patch.object(doctor, "diagnose", return_value={
                "checks": [], "ok": True, "blocking": [], "auto_fixable": [],
                "summary": "0/0 briques opérationnelles", "data_dir": "x"}):
                report = doctor.full_diagnose()
        # La structure du contrat JSON est respectée.
        parsed = json.loads(json.dumps(report, ensure_ascii=False))
        self.assertIn(parsed["status"], ("ok", "warning", "error"))
        self.assertIsInstance(parsed["checks"], list)
        self.assertTrue(parsed["checks"])
        for check in parsed["checks"]:
            self.assertIn(check["status"], ("ok", "warning", "error"))
            self.assertIn("name", check)
        names = {c["name"] for c in parsed["checks"]}
        self.assertIn("llm_server", names)
        self.assertIn("tool_registry", names)
        # Un warning seul ne doit jamais être bloquant.
        self.assertEqual(report["blocking"], [])


class StrictModeTests(unittest.TestCase):
    def test_code_de_sortie_warnings_seuls(self):
        report = {"blocking": [], "warnings": 3, "internal_error": False}
        self.assertEqual(doctor._exit_code(report), 0)

    def test_code_de_sortie_bloquant(self):
        report = {"blocking": ["database"], "warnings": 0, "internal_error": False}
        self.assertEqual(doctor._exit_code(report), 1)

    def test_code_de_sortie_erreur_interne(self):
        self.assertEqual(doctor._exit_code({"internal_error": "boom"}), 2)

    def test_cli_mode_strict_sans_acces_reseau(self):
        """Le CLI complet doit s'exécuter sans jamais lever, même réseau coupé."""
        with mock.patch.object(doctor.sys, "version_info", (3, 12, 3)), \
             mock.patch.object(doctor, "probe_imports", return_value={
            name: {"ok": True} for name in doctor.CRITICAL_IMPORTS}), \
             mock.patch.object(doctor, "probe_tools", return_value={
                 "total": 1, "duplicates": [], "no_handler": [],
                 "risks": {"read_only": 1}, "registered": 1}), \
             mock.patch.object(doctor, "_llm_endpoints", return_value=[
                 ("openai", "http://127.0.0.1:8080/v1")]), \
             mock.patch.object(doctor, "probe_llm", return_value=[
                 {"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                  "status": "online", "models": ["qwen2.5-coder-14b-act"], "error": ""}]), \
             mock.patch.object(doctor, "_port_state", return_value={
                 "port": 8765, "state": "free"}), \
             mock.patch.object(doctor, "_sqlite_ro", side_effect=Exception("no db")), \
             mock.patch.object(doctor, "DB_PATH",
                               os.path.join(tempfile.gettempdir(), "doctor_absent.db")), \
             mock.patch.object(doctor, "_sys_check_core", return_value=(
                 {"name": "core", "label": "Core startup", "status": doctor.OK,
                  "detail": "amorcé", "recommendation": "", "blocking": False,
                  "details": {}}, mock.Mock())), \
             mock.patch.object(doctor, "diagnose", return_value={
                 "checks": [], "ok": True, "blocking": [], "auto_fixable": [],
                 "summary": "0/0", "data_dir": "x"}):
            with mock.patch.object(doctor.sys, "argv",
                                   ["doctor", "--strict", "--json"]), \
                 mock.patch.object(doctor.sys, "exit") as fake_exit, \
                 mock.patch("builtins.print") as fake_print:
                doctor.cli()
        # --strict : warnings seuls (base absente) → code de sortie 0.
        fake_exit.assert_called_once_with(0)
        # Le mode --json ne laisse AUCUN texte parasite : la sortie est
        # intégralement du JSON strictement valide (bannière comprise).
        printed = fake_print.call_args.args[0]
        payload = json.loads(printed)  # lève si un texte parasite s'y est glissé
        self.assertEqual(payload["status"], "warning")


class SeverityExitCodeTests(unittest.TestCase):
    """Les niveaux de criticité pilotent réellement le code de sortie :

    0 si HEALTHY ou DEGRADED sans erreur bloquante · 1 seulement si BLOCKED.
    """

    def test_optional_offline_exit_0(self):
        check = doctor._check("vision", "Vue", False, severity=doctor.OPTIONAL,
                              detail="aucun modèle vision")
        converted = doctor._brique_to_result(check)
        self.assertEqual(converted["status"], doctor.WARNING)
        self.assertEqual(converted["severity"], doctor.OPTIONAL)
        self.assertFalse(converted["required"])
        self.assertFalse(converted["blocking"])
        self.assertEqual(doctor._exit_code({"blocking": [], "internal_error": False}), 0)

    def test_degraded_offline_exit_0(self):
        check = doctor._check("comfyui", "ComfyUI", False, severity=doctor.DEGRADED,
                              detail="hors ligne")
        converted = doctor._brique_to_result(check)
        self.assertEqual(converted["status"], doctor.WARNING)
        self.assertFalse(converted["required"])
        self.assertFalse(converted["blocking"])
        self.assertEqual(doctor._exit_code({"blocking": [], "warnings": 1,
                                            "internal_error": False}), 0)

    def test_required_offline_exit_1(self):
        check = doctor._check("llm", "Modèle de langage", False, severity=doctor.BLOCKING,
                              detail="aucun fournisseur")
        converted = doctor._brique_to_result(check)
        self.assertEqual(converted["status"], doctor.ERROR)
        self.assertTrue(converted["required"])
        self.assertTrue(converted["blocking"])
        self.assertEqual(doctor._exit_code({"blocking": ["llm"],
                                            "internal_error": False}), 1)

    def test_plusieurs_warnings_seuls_exit_0(self):
        report = {"blocking": [], "warnings": 5, "errors": 0, "internal_error": False}
        self.assertEqual(doctor._exit_code(report), 0)

    def test_erreur_interne_doctor_exit_2(self):
        self.assertEqual(doctor._exit_code({"internal_error": "boom"}), 2)


class LlmFallbackTests(unittest.TestCase):
    """llama.cpp (8080) éteint mais un autre provider opérationnel → PAS bloquant."""

    def _offline_probe(self):
        return [{"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                 "status": "offline", "models": [], "error": "refused"}]

    def test_provider_alternatif_connecte_empeche_faux_bloquant(self):
        provider_status = [
            {"id": "c1", "type": "ollama", "name": "Ollama", "connected": True,
             "models": ["qwen2.5-coder-14b-act"], "detail": "1 modèle(s)"},
            {"id": "c2", "type": "openai", "name": "llama.cpp", "connected": False,
             "models": [], "detail": "Aucun modèle / clé invalide"},
        ]
        server = doctor._sys_check_llm_server(self._offline_probe(), provider_status)
        model = doctor._sys_check_llm_model(self._offline_probe(), provider_status)
        self.assertEqual(server["status"], doctor.OK)
        self.assertFalse(server["blocking"])
        self.assertIn("Ollama", server["detail"])
        self.assertEqual(model["status"], doctor.OK)
        self.assertIn("qwen2.5-coder-14b-act", model["detail"])

    def test_provider_status_tous_offline_bloque(self):
        provider_status = [
            {"id": "c1", "type": "ollama", "name": "Ollama", "connected": False,
             "models": [], "detail": "Aucun modèle"},
            {"id": "c2", "type": "openai", "name": "llama.cpp", "connected": False,
             "models": [], "detail": "Aucun modèle / clé invalide"},
        ]
        server = doctor._sys_check_llm_server(self._offline_probe(), provider_status)
        self.assertEqual(server["status"], doctor.ERROR)
        self.assertTrue(server["blocking"])
        self.assertTrue(server["required"])
        self.assertEqual(doctor._exit_code({"blocking": ["llm_server"],
                                            "internal_error": False}), 1)

    def test_aucun_provider_configuré_est_bloquant(self):
        provider_status = [
            {"id": "", "type": "ollama", "name": "Ollama", "connected": False,
             "models": [], "detail": "Not configured"},
        ]
        server = doctor._sys_check_llm_server([], provider_status)
        self.assertEqual(server["status"], doctor.ERROR)
        self.assertTrue(server["blocking"])

    def test_provider_status_illisible_revient_aux_sondes(self):
        # Un statut non-listable ne doit pas faire planter : repli sur les sondes.
        result = doctor._sys_check_llm_server(self._offline_probe(), None)
        self.assertEqual(result["status"], doctor.ERROR)
        self.assertTrue(result["blocking"])


class JsonSeverityTests(unittest.TestCase):
    def test_chaque_check_expose_severite_required_message(self):
        for check in (doctor._sys_result("comfyui", doctor.WARNING, "hors ligne",
                                         severity=doctor.DEGRADED),
                      doctor._sys_result("llm_server", doctor.ERROR, "éteint",
                                         severity=doctor.BLOCKING),
                      doctor._brique_to_result(doctor._check(
                          "vision", "Vue", False, severity=doctor.OPTIONAL,
                          detail="aucun modèle vision"))):
            payload = json.loads(json.dumps(check, ensure_ascii=False))
            self.assertIn("name", payload)
            self.assertIn("status", payload)
            self.assertIn("severity", payload)
            self.assertIn("required", payload)
            self.assertIn("message", payload)
        self.assertEqual(doctor._sys_result("comfyui", doctor.WARNING,
                                            "hors ligne")["severity"], doctor.DEGRADED)
        self.assertEqual(doctor._sys_result("llm_server", doctor.ERROR,
                                            "éteint", blocking=True)["severity"],
                         doctor.BLOCKING)

    def test_json_severite_correcte_sur_rapport_simule(self):
        """Health + severity agrégés sur un rapport complet, sans accès réseau."""
        def core_result():
            return ({"name": "core", "label": "Core startup", "status": doctor.OK,
                     "detail": "amorcé", "recommendation": "", "blocking": False,
                     "details": {}}, mock.Mock())
        with mock.patch.object(doctor, "probe_imports", return_value={
            name: {"ok": True} for name in doctor.CRITICAL_IMPORTS}), \
             mock.patch.object(doctor, "probe_tools", return_value={
                 "total": 5, "duplicates": [], "no_handler": [],
                 "risks": {"read_only": 5}, "registered": 5}), \
             mock.patch.object(doctor, "_llm_endpoints", return_value=[
                 ("openai", "http://127.0.0.1:8080/v1")]), \
             mock.patch.object(doctor, "probe_llm", return_value=[
                 {"type": "openai", "url": "http://127.0.0.1:8080/v1/models",
                  "status": "offline", "models": [], "error": "refused"}]), \
             mock.patch.object(doctor, "_port_state", return_value={
                 "port": 8765, "state": "free"}), \
             mock.patch.object(doctor, "_sqlite_ro", side_effect=Exception("no db")), \
             mock.patch.object(doctor, "DB_PATH",
                               os.path.join(tempfile.gettempdir(), "doctor_absent.db")):
            # LLMManager répond : aucun provider joignable → llm BLOCKING (REQUIRED).
            class FakeLlm:
                def status(self, **kwargs):
                    return [{"id": "c1", "type": "ollama", "name": "Ollama",
                             "connected": False, "models": [],
                             "detail": "Aucun modèle / clé invalide"}]
            core = mock.Mock()
            core.llm = FakeLlm()
            with mock.patch.object(doctor, "_sys_check_core", return_value=core_result()), \
                 mock.patch.object(doctor, "diagnose", return_value={
                     "checks": [], "ok": True, "blocking": [], "auto_fixable": [],
                     "summary": "0/0", "data_dir": "x"}):
                report = doctor.full_diagnose()
        parsed = json.loads(json.dumps(report, ensure_ascii=False))
        # LLM requis et injoignable → BLOCKED + exit 1.
        self.assertEqual(parsed["health"], doctor.BLOCKED)
        self.assertIn("llm_server", report["blocking"])
        by_name = {c["name"]: c for c in parsed["checks"]}
        self.assertEqual(by_name["llm_server"]["severity"], doctor.BLOCKING)
        self.assertTrue(by_name["llm_server"]["required"])
        self.assertTrue(by_name["llm_server"]["blocking"])
        self.assertIn("message", by_name["llm_server"])


if __name__ == "__main__":
    unittest.main()
