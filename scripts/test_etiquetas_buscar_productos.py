"""Regresión búsqueda etiquetas: CORREA + TERRALORD (modelo en campo MODELO)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from app.bodega.routes import _buscar_productos_para_etiquetas


def test_correa_terralord_token_search() -> None:
    app = create_app()
    with app.app_context():
        items = _buscar_productos_para_etiquetas("CORREA TERRALORD", limit=40)
        codes = {i["codigo"] for i in items}
        assert "TE0002" in codes, f"TE0002 missing in {codes}"
        assert "TE2401" in codes, f"TE2401 missing in {codes}"
        assert "MX1004" in codes, f"MX1004 missing in {codes}"
        assert len(items) >= 3
        print("OK correa terralord", sorted(codes))


def test_terralord_single_word() -> None:
    app = create_app()
    with app.app_context():
        items = _buscar_productos_para_etiquetas("TERRALORD", limit=10)
        assert len(items) >= 3
        print("OK terralord single", len(items))


if __name__ == "__main__":
    test_correa_terralord_token_search()
    test_terralord_single_word()
