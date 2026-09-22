"""FASE 8.x — que ningun artefacto en disco pueda mentir sobre su medicion.

Dos trampas reales. La primera me atrapo a mi en esta misma sesion: lei
``fase81_final_report.an0-pv0.json`` como si fuera una corrida determinista
recien hecha, y era el informe LLM de la tercera A/B, tres horas mas viejo.

1. **Esquema de brazo supersedido.** Al anadir la dimension ``pr``, los ficheros
   ``an0-pv0`` / ``an1-pv0`` dejaron de corresponder a ningun brazo que el
   harness produzca, pero siguen en el mismo directorio con el mismo prefijo.
   Son historia legitima; lo que no puede pasar es que se confundan.

2. **Validez indeterminable.** ``measurement_valid`` se anadio DESPUES de las
   corridas del 2026-09-20, asi que el unico brazo valido —an0-pv0-pr0, 74/76—
   no lo declara, mientras los dos invalidos si dicen ``False``. Un campo
   ausente leido como "valido" es exactamente el error que el guard existe para
   evitar. Cuando falta se RECALCULA desde los runs del propio artefacto
   contando ``llm_unavailable``: es una medicion, no una suposicion.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from evals.fase81g_closure import _all_arm_ids, arm_id, probe_artifact_integrity


def _report(out: Path, name: str, *, arm: str, llm: bool, valid=None,
            passed: int = 70, n: int = 76) -> None:
    bench = {"pass": passed, "n": n} if llm else {}
    if valid is not None:
        bench["measurement_valid"] = valid
    (out / name).write_text(json.dumps({
        "generated_at": "2026-09-20T12:00:00", "llm_executed": llm,
        "arm": {"arm": arm}, "benchmark": bench}), encoding="utf-8")


def _runs(out: Path, arm: str, *, vacias: int, llenas: int) -> None:
    lines = [json.dumps({"id": f"E{i:02d}", "fallback_reason": "llm_unavailable"})
             for i in range(vacias)]
    lines += [json.dumps({"id": f"F{i:02d}", "fallback_reason": None})
              for i in range(llenas)]
    (out / f"fase81g_runs_llm.{arm}.jsonl").write_text(
        "\n".join(lines), encoding="utf-8")


class ValidityIsRecoveredFromTheArtifactsOwnEvidenceTests(unittest.TestCase):

    def test_a_missing_field_is_recomputed_not_assumed(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, f"fase81_final_report.{arm_id()}.json",
                    arm=arm_id(), llm=True, passed=74)
            _runs(out, arm_id(), vacias=0, llenas=76)
            row = probe_artifact_integrity(out)["artifacts"][0]
        self.assertTrue(row["measurement_valid"])
        self.assertIn("recalculado", row["validity_source"])

    def test_a_degraded_run_is_recomputed_as_invalid(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, f"fase81_final_report.{arm_id()}.json",
                    arm=arm_id(), llm=True, passed=18)
            _runs(out, arm_id(), vacias=59, llenas=17)
            r = probe_artifact_integrity(out)
        self.assertFalse(r["artifacts"][0]["measurement_valid"])
        self.assertEqual(r["current_valid_arms"], [])

    def test_a_declared_field_is_trusted_over_recomputation(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, f"fase81_final_report.{arm_id()}.json",
                    arm=arm_id(), llm=True, valid=False)
            row = probe_artifact_integrity(out)["artifacts"][0]
        self.assertFalse(row["measurement_valid"])
        self.assertEqual(row["validity_source"], "declarado")

    def test_a_current_report_whose_validity_cannot_be_established_fails(self):
        """Sin runs y sin campo: no se sabe, y no saberlo es el fallo."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, f"fase81_final_report.{arm_id()}.json",
                    arm=arm_id(), llm=True)
            r = probe_artifact_integrity(out)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue(r["validity_indeterminable"])


class SupersededArtifactsAreListedNotHiddenTests(unittest.TestCase):

    def test_an_old_arm_scheme_is_flagged(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, "fase81_final_report.an0-pv0.json",
                    arm="an0-pv0", llm=True, passed=72)
            _runs(out, "an0-pv0", vacias=0, llenas=76)
            r = probe_artifact_integrity(out)
        self.assertEqual(r["superseded_arm_scheme"],
                         ["fase81_final_report.an0-pv0.json"])
        self.assertNotIn("an0-pv0", r["current_valid_arms"])

    def test_history_alone_does_not_fail_the_probe(self):
        """Una alarma que suena para siempre por un fichero viejo es la misma
        clase de metrica que este bloque lleva once defectos eliminando."""
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            _report(out, "fase81_final_report.json", arm=None, llm=True,
                    passed=66, n=69)  # sin runs: validez indeterminable
            self.assertEqual(probe_artifact_integrity(out)["verdict"], "PASS")

    def test_the_current_scheme_enumerates_every_flag_combination(self):
        """El numero crece con cada dimension; lo que no puede cambiar es que se
        GENERE de la tabla de banderas en vez de escribirse."""
        from evals.fase81g_closure import ARM_DIMENSIONS

        vigentes = set(_all_arm_ids())
        esperado = 2 ** len(ARM_DIMENSIONS)
        self.assertEqual(len(vigentes), esperado)
        self.assertNotIn("an0-pv0", vigentes)
        self.assertIn(arm_id(), vigentes)

    def test_an_unreadable_artifact_is_reported_not_swallowed(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "fase81_final_report.roto.json").write_text("{{{", encoding="utf-8")
            rows = probe_artifact_integrity(out)["artifacts"]
        self.assertEqual(rows[0]["error"], "ilegible")


class TheRealDirectoryIsCoherentTests(unittest.TestCase):
    """Contra el directorio de verdad, no contra uno sintetico.

    Los artefactos de medicion NO se versionan —llevan datos de negocio y son
    regenerables—, asi que en un clon limpio este directorio esta vacio. Estas
    pruebas se saltan cuando no hay nada que auditar en vez de afirmar sobre el
    estado local de una maquina: un test que solo pasa en el portatil de quien
    lo escribio no prueba nada, y colarlo en el checkpoint seria justo la clase
    de artefacto que miente sobre una medicion.
    """

    def _probe(self):
        from evals.fase81g_closure import OUT_DIR

        out = Path(OUT_DIR)
        if not out.is_dir() or not any(out.glob("fase81_final_report*.json")):
            self.skipTest("sin artefactos de medicion en disco (clon limpio)")
        return probe_artifact_integrity(out)

    def test_the_live_artifacts_pass_the_integrity_probe(self):
        r = self._probe()
        self.assertEqual(r["verdict"], "PASS", r["current_llm_reports_without_validity"])

    def test_every_local_llm_report_has_a_determinable_validity(self):
        """Lo que se puede afirmar sin conocer QUE corridas hay en la maquina:
        de todas ellas se sabe si midieron o no."""
        r = self._probe()
        llm = [a for a in r["artifacts"] if a.get("llm_executed")
               and a.get("scheme") == "vigente"]
        if not llm:
            self.skipTest("sin informes LLM del esquema vigente")
        for row in llm:
            with self.subTest(file=row["file"]):
                self.assertIsNotNone(row["measurement_valid"])
                self.assertNotEqual(row["validity_source"], "indeterminable")


if __name__ == "__main__":
    unittest.main()
