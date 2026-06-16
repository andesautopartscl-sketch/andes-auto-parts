"""Regresión de parsers OCR de facturas (sin red ni Vision API).

Ejecutar SIEMPRE antes de tocar:
  - app/utils/invoice_vision.py
  - app/utils/invoice_providers/*.py

Uso:
  .venv\\Scripts\\python.exe scripts\\test_invoice_parsers_regression.py

Si agregás un proveedor o corregís un caso, añadí el fixture en el módulo
correspondiente (test_mundo_repuestos_ocr.py, test_xinwang_ocr.py, etc.)
y registralo en SUITES de este archivo.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SuiteFn = Callable[[], None]

SUITES: list[tuple[str, SuiteFn]] = []


def _load_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _register(mod, fn_name: str, label: str) -> None:
    def run() -> None:
        getattr(mod, fn_name)()

    SUITES.append((label, run))


def main() -> int:
    mundo = _load_module("test_mundo_repuestos_ocr", "test_mundo_repuestos_ocr.py")
    for fn_name in (
        "test_fecha_dte_xml",
        "test_fecha_emision_mundo_multiline",
        "test_fecha_emision_mundo_columnar_lejos",
        "test_fecha_emision_123310",
        "test_fixture",
        "test_fixture_123310",
    ):
        _register(mundo, fn_name, f"mundo.{fn_name}")

    xinwang = _load_module("test_xinwang_ocr", "test_xinwang_ocr.py")
    _register(xinwang, "test_fixture_qty", "xinwang.fixture_qty")

    parachoque = _load_module("_test_xinwang_parachoque", "_test_xinwang_parachoque.py")
    _register(parachoque, "main", "xinwang.parachoque")

    ali = _load_module("test_ali_repuestos_ocr", "test_ali_repuestos_ocr.py")
    for fn_name in (
        "test_fixture_34084_productos",
        "test_fixture_single_item_descuento",
    ):
        _register(ali, fn_name, f"ali.{fn_name}")

    tecnicor = _load_module("test_tecnicor_ocr", "test_tecnicor_ocr.py")
    _register(tecnicor, "test_fixture_3636124", "tecnicor.fixture_3636124")

    rc = _load_module("test_repuesto_center_ocr", "test_repuesto_center_ocr.py")
    for fn_name in ("test_fixture_564465", "test_repair_folio_en_neto"):
        _register(rc, fn_name, f"repuesto_center.{fn_name}")

    huoying = _load_module("test_huoying_ocr", "test_huoying_ocr.py")
    _register(huoying, "test_fixture_39039", "huoying.fixture_39039")

    failed: list[str] = []
    print("=" * 72)
    print("REGRESIÓN PARSERS OCR (fixtures locales)")
    print("=" * 72)

    for label, fn in SUITES:
        try:
            fn()
            print(f"  PASS  {label}")
        except Exception as exc:
            failed.append(label)
            print(f"  FAIL  {label}: {exc}")
            traceback.print_exc()

    print("=" * 72)
    if failed:
        print(f"FALLÓ: {len(failed)} suite(s): {', '.join(failed)}")
        return 1
    print(f"OK: {len(SUITES)} suite(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
