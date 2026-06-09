from __future__ import annotations

import unicodedata

from sqlalchemy import text


def _fts_norm(s: str) -> str:
    s = (s or "").strip().lower()
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if unicodedata.category(c) != "Mn")


def _fts_blob(
    codigo,
    codigo_oem,
    codigo_alternativo,
    descripcion,
    modelo,
    motor,
    marca,
    medidas,
    homologados,
) -> str:
    fields = [
        codigo,
        codigo_oem,
        codigo_alternativo,
        descripcion,
        modelo,
        motor,
        marca,
        medidas,
        homologados,
    ]
    return " ".join(_fts_norm(f or "") for f in fields)


def fts_blob_de_producto(p) -> str:
    return _fts_blob(
        p.codigo,
        p.codigo_oem,
        p.codigo_alternativo,
        p.descripcion,
        p.modelo,
        p.motor,
        p.marca,
        p.medidas,
        p.homologados,
    )


def fts_create_table(conn) -> None:
    """Crea la tabla FTS5 si no existe."""
    conn.execute(
        text(
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS productos_fts USING fts5(
            codigo UNINDEXED,
            blob,
            tokenize='unicode61 remove_diacritics 2'
        )
    """
        )
    )


def fts_rebuild(conn) -> int:
    """Reconstruye el índice completo desde productos activos."""
    conn.execute(text("DELETE FROM productos_fts"))
    rows = conn.execute(
        text(
            """
        SELECT CODIGO, "CODIGO OEM", "CODIGO ALTERNATIVO O ANTIGUO",
               DESCRIPCION, MODELO, MOTOR, MARCA, medidas, HOMOLOGADOS
        FROM productos WHERE ACTIVO = 1
    """
        )
    ).fetchall()
    for row in rows:
        blob = _fts_blob(*row)
        conn.execute(
            text("INSERT INTO productos_fts(codigo, blob) VALUES (:c, :b)"),
            {"c": row[0], "b": blob},
        )
    return len(rows)


def fts_upsert(conn, codigo: str, blob: str) -> None:
    conn.execute(
        text("DELETE FROM productos_fts WHERE codigo = :c"),
        {"c": codigo},
    )
    conn.execute(
        text("INSERT INTO productos_fts(codigo, blob) VALUES (:c, :b)"),
        {"c": codigo, "b": blob},
    )


def fts_delete(conn, codigo: str) -> None:
    conn.execute(
        text("DELETE FROM productos_fts WHERE codigo = :c"),
        {"c": codigo},
    )
