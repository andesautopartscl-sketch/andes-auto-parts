"""FASE 8.8 — cruce OEM, y la coherencia de límites entre capas.

Dónde estaban los datos, medido antes de escribir nada: las tablas relacionales
que parecían el sitio natural están VACÍAS (``oems`` 0, ``compatibilidades`` 0,
``vehiculos`` 0, ``motores`` 0). Los datos viven desnormalizados en
``productos``: ``CODIGO OEM`` 72%, ``MODELO``/``MARCA`` 99%, ``MOTOR`` 94%.
Construir sobre las tablas vacías habría repetido el error de 8.6.

Tres decisiones que estos tests protegen:

1. **Comparación normalizada.** ``0 280 751 089`` (Bosch) es UN código con
   espacios y ``038-1701225`` lleva guión. Con igualdad literal, el 72% de los
   datos sería inalcanzable por quien teclee el código de forma natural.

2. **HOMOLOGADOS no son códigos.** Son aplicaciones de vehículo
   (``DFM K07 1.3,GAC GONOW 1.3``). Mezclarlos haría que el agente ofreciera un
   coche donde el usuario espera una pieza.

3. **El límite se declaraba dos veces y no coincidía.** Medido: el orquestador
   admitía ``limit=50`` en cuatro tools y el Gateway las rechazaba con 400. Un
   plan válido moría en la frontera exterior, e ``invalid_args`` es uno de los
   ejes que mide el benchmark.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# Al FINAL: andes_agent/ tiene su propio paquete `tests` y anteponerlo
# eclipsaria el tests/ del proyecto.
if str(_ROOT / "andes_agent") not in sys.path:
    sys.path.append(str(_ROOT / "andes_agent"))

from app.assistant.orchestrator.arg_schema import ArgSchemaError, validate_tool_args
from app.assistant.orchestrator.catalog import ALLOWED_TOOLS
from app.assistant.orchestrator.goal_coverage import REQUIREMENT_TYPES, GoalCoverage
from app.assistant.orchestrator.tool_contracts import TOOL_CONTRACTS
from app.internal_agent.equivalences import (
    MIN_KEY_LEN,
    normalize_code,
    split_applications,
    split_codes,
    validate_equivalence_args,
)


class CodeNormalisationTests(unittest.TestCase):
    """Los separadores no son parte del código; el usuario no los teclea igual."""

    def test_separators_collapse(self):
        for raw in ("0 280 751 089", "0-280-751-089", "0.280.751.089",
                    "0280751089", "0280 751-089"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_code(raw), "0280751089")

    def test_case_is_folded(self):
        self.assertEqual(normalize_code("fk1284"), normalize_code("FK1284"))

    def test_a_space_never_splits_a_code(self):
        """Medido: 973 filas llevan espacios DENTRO del código. Partir por
        espacios convertiría un Bosch en cuatro códigos inventados."""
        self.assertEqual(split_codes("0 280 751 089"), ["0 280 751 089"])

    def test_a_slash_does_split(self):
        """Medido: 254 filas usan ' / ' para separar códigos de verdad."""
        self.assertEqual(split_codes("4013003400 / 4013003500"),
                         ["4013003400", "4013003500"])

    def test_a_trailing_separator_yields_no_empty_code(self):
        self.assertEqual(split_codes("269832600118/"), ["269832600118"])

    def test_applications_are_comma_separated(self):
        self.assertEqual(
            split_applications("DFM K07 1.3,GAC GONOW 1.3,CM10"),
            ["DFM K07 1.3", "GAC GONOW 1.3", "CM10"])

    def test_applications_land_in_their_own_field(self):
        """El error fácil del dominio: 'GAC GONOW 1.3' es un coche, no una pieza.

        La separación NO la hacen los splitters —ambos parten por coma— sino la
        COLUMNA de la que se lee cada uno y el campo en el que aterrizan. Este
        test fija esa frontera, que es la que de verdad protege."""
        from app.internal_agent.equivalences import APP_COLUMN, OEM_COLUMN, _row

        class _Rec:
            codigo = "FK1264"
            descripcion = "ANILLO"
            marca = "FK"
            modelo = "X"
            motor = ""
            oem = "038-1701225"
            alternativo = ""
            aplicaciones = "DFM K07 1.3,GAC GONOW 1.3"

        item = _row(_Rec())
        self.assertEqual(item["oem"], ["038-1701225"])
        self.assertEqual(item["aplicaciones"], ["DFM K07 1.3", "GAC GONOW 1.3"])
        self.assertNotIn("GAC GONOW 1.3", item["oem"] + item["alternativos"])
        self.assertNotEqual(OEM_COLUMN, APP_COLUMN)


class AnchorRequiredTests(unittest.TestCase):
    """Sin ancla la consulta devolvería un recorte del catálogo, y el agente lo
    presentaría como 'equivalencias'. Eso sería falso."""

    def test_no_anchor_is_rejected(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            validate_equivalence_args({"marca": "BOSCH"})

    def test_an_oem_anchor_is_enough(self):
        self.assertEqual(validate_equivalence_args({"oem": "038-1701225"})["oem"],
                         "038-1701225")

    def test_a_code_anchor_is_enough(self):
        self.assertEqual(validate_equivalence_args({"codigo": "fk1284"})["codigo"],
                         "FK1284")

    def test_a_key_too_short_is_rejected(self):
        """Una clave de 2 caracteres empareja con medio catálogo: eso ya no es
        una equivalencia, es ruido presentado como resultado."""
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            validate_equivalence_args({"oem": "ab"})
        self.assertGreaterEqual(MIN_KEY_LEN, 4)

    def test_sql_in_a_term_is_rejected(self):
        from app.internal_agent.m2m import InternalAuthError

        with self.assertRaises(InternalAuthError):
            validate_equivalence_args({"oem": "1' UNION SELECT * FROM usuarios --"})

    def test_the_orchestrator_enforces_the_anchor_too(self):
        with self.assertRaises(ArgSchemaError):
            validate_tool_args("get_equivalences", {"marca": "BOSCH"})


class ToolSurfaceTests(unittest.TestCase):
    def _gateway(self):
        from andes_agent.tools.registry import ALLOWED_TOOLS as GW

        return GW

    def test_the_tool_is_in_every_allowlist(self):
        self.assertIn("get_equivalences", ALLOWED_TOOLS)
        self.assertIn("get_equivalences", TOOL_CONTRACTS)
        self.assertIn("get_equivalences", self._gateway())
        self.assertIn("equivalences", REQUIREMENT_TYPES)

    def test_the_allowlists_still_agree(self):
        self.assertEqual(set(ALLOWED_TOOLS), set(TOOL_CONTRACTS))
        self.assertEqual(set(ALLOWED_TOOLS), set(self._gateway()))

    def test_still_no_write_tool(self):
        self.assertEqual([n for n, s in self._gateway().items() if s.write], [])

    def test_the_tool_declares_no_financial_field(self):
        """El cruce es técnico. Si algún día trajera precio habría que pasarlo
        por include_finance, y este test obliga a notarlo."""
        from andes_agent.tools import get_equivalences as mod

        joined = " ".join(mod.PUBLIC_ITEM_FIELDS + mod.PUBLIC_LIST_FIELDS)
        for money in ("precio", "costo", "margen", "subtotal"):
            self.assertNotIn(money, joined, money)


class LimitCoherenceTests(unittest.TestCase):
    """Un plan válido en una capa no puede morir en la siguiente."""

    _ARGS = {
        "search_catalog": {"q": "filtro"}, "get_product": {"codigo": "2404"},
        "get_inventory": {"codigo": "2404"},
        "check_stock": {"items": [{"codigo": "2404", "cantidad": 1}]},
        "get_stock_movements": {"codigo": "2404"},
        "get_ingresos": {"codigo": "2404"}, "get_purchase_orders": {},
        "get_customer": {"q": "a"}, "get_supplier": {"q": "a"},
        "get_sales": {}, "get_equivalences": {"oem": "038-1701225"},
        "get_dashboard_kpis": {},
    }

    def _max_limit(self, fn, tool: str) -> int:
        args = self._ARGS.get(tool, {})
        highest = 0
        for candidate in range(1, 101):
            try:
                fn(tool, {**args, "limit": candidate})
                highest = candidate
            except Exception:  # noqa: BLE001 — se busca la frontera, no el error
                pass
        return highest

    def test_the_orchestrator_never_proposes_more_than_the_gateway_accepts(self):
        """El defecto medido: limit=50 pasaba aquí y el Gateway lo rechazaba con
        400. Cuatro tools lo hacían."""
        from andes_agent.schemas import validate_tool_arguments

        offenders = []
        for tool in sorted(ALLOWED_TOOLS):
            inner = self._max_limit(validate_tool_args, tool)
            outer = self._max_limit(validate_tool_arguments, tool)
            if inner > outer:
                offenders.append((tool, inner, outer))
        self.assertEqual(offenders, [], f"limites incoherentes: {offenders}")

    def test_the_declared_contract_matches_what_is_enforced(self):
        """Lo que el prompt le promete al modelo tiene que ser ejecutable."""
        offenders = []
        for tool, contract in TOOL_CONTRACTS.items():
            declared = ((contract.get("optional") or {}).get("limit") or {}).get("max")
            if declared is None:
                continue
            enforced = self._max_limit(validate_tool_args, tool)
            if enforced and declared > enforced:
                offenders.append((tool, declared, enforced))
        self.assertEqual(offenders, [], f"contrato promete de mas: {offenders}")


class EquivalenceRequirementTests(unittest.TestCase):
    def _types(self, message: str) -> list[str]:
        snap = GoalCoverage.from_message(message).safe_snapshot()
        return [r["type"] for r in snap["requirements"]]

    def test_an_oem_question_is_detected(self):
        self.assertIn("equivalences",
                      self._types("Que codigo tenemos para el OEM 038-1701225?"))

    def test_alternatives_in_both_genders(self):
        self.assertIn("equivalences", self._types("Que alternativas tengo para el 2404?"))
        self.assertIn("equivalences", self._types("Dame un codigo alternativo"))

    def test_homologated_is_detected(self):
        self.assertIn("equivalences", self._types("Dame los homologados del 2404"))

    def test_a_plain_stock_question_is_untouched(self):
        self.assertEqual(self._types("Stock del 2404"), ["current_inventory"])

    def test_equivalences_and_stock_coexist(self):
        types = self._types("Equivalentes del 2404 y cuanto stock queda")
        self.assertIn("equivalences", types)
        self.assertIn("current_inventory", types)


class EquivalenceViewTests(unittest.TestCase):
    def _view(self, data: dict) -> dict:
        from app.assistant.orchestrator.answer_view import build_answer_view
        from app.assistant.orchestrator.evidence_store import EvidenceStore

        store = EvidenceStore()
        store.add_from_tool_result(
            tool="get_equivalences", arguments={"oem": "038-1701225"},
            result={"ok": True, "empty": False, "meta": {}, "data": data})
        return build_answer_view(store).as_dict()

    def test_each_equivalent_becomes_a_navigable_card(self):
        block = self._view({"items": [
            {"codigo": "FK1264", "descripcion": "ANILLO", "marca": "FK",
             "modelo": "X", "motor": "", "oem": ["038-1701225"],
             "alternativos": [], "aplicaciones": ["RICH 6 2.5"]}],
            "count": 1})["blocks"][0]
        card = block["cards"][0]
        self.assertEqual(card["title"], "FK1264")
        self.assertEqual(card["ref"], "FK1264")

    def test_applications_never_render_as_part_fields(self):
        """Una aplicación en la tarjeta se leería como una pieza equivalente."""
        blob = json.dumps(self._view({"items": [
            {"codigo": "FK1264", "descripcion": "ANILLO", "marca": "FK",
             "modelo": "X", "motor": "", "oem": [], "alternativos": [],
             "aplicaciones": ["GAC GONOW 1.3"]}], "count": 1}), ensure_ascii=False)
        self.assertNotIn("GAC GONOW", blob)


class EquivalenceContextTests(unittest.TestCase):
    def test_the_queried_oem_is_remembered(self):
        from app.assistant.orchestrator.conversation_context import (
            extract_entities_from_evidence)

        ents = extract_entities_from_evidence([{
            "tool": "get_equivalences", "ok": True, "meta": {},
            "data": {"query": {"oem": "038-1701225"}, "matched_on": "oem",
                     "items": [{"codigo": "FK1264"}, {"codigo": "FK1284"}],
                     "count": 2}}])
        self.assertEqual(ents["oem"], "038-1701225")
        self.assertIn("FK1264", ents["codigos"])

    def test_the_oem_reaches_the_model(self):
        from app.assistant.orchestrator.llm.agent_prompts import build_agent_context_note

        note = build_agent_context_note(
            {"resolved_entities": {"oem": "038-1701225", "codigo": "FK1264"}})
        self.assertIn("038-1701225", note)


if __name__ == "__main__":
    unittest.main()
