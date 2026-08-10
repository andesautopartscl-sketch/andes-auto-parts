from __future__ import annotations

import unicodedata

from sqlalchemy import text

# Súbela al cambiar los campos que alimentan el blob: el índice se reconstruye solo.
FTS_BLOB_VERSION = 2


def _fts_norm(s: str) -> str:
    s = (s or "").strip().lower()
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if unicodedata.category(c) != "Mn")


def _fts_escape_token(t: str) -> str:
    return (t or "").replace('"', '""')


def _fts_match_token(raw: str) -> str:
    """
    Término FTS5 con prefijo (*) para tolerar singular/plural y búsquedas parciales.
    Ej.: pastilla → pastillas, past → pastillas.
    """
    t = _fts_norm(raw)
    if not t:
        return ""
    esc = _fts_escape_token(t)
    if len(t) >= 2:
        if esc.isalnum():
            return f"{esc}*"
        return f'"{esc}"*'
    return f'"{esc}"' if not esc.isalnum() else esc


def fts_match_query(palabras: list[str]) -> str:
    """AND de términos FTS5 (cada uno con prefijo cuando aplica)."""
    return " ".join(p for p in (_fts_match_token(w) for w in palabras) if p)


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
    anio=None,
    version=None,
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
        anio,
        version,
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
        getattr(p, "anio", None),
        getattr(p, "version", None),
    )


def _fts_codes_blob(codigo, codigo_oem, codigo_alternativo, homologados) -> str:
    """Solo columnas de código: se consultan por subcadena, no por palabra."""
    campos = [codigo, codigo_oem, codigo_alternativo, homologados]
    return " ".join(_fts_norm(f or "") for f in campos)


def fts_codes_blob_de_producto(p) -> str:
    return _fts_codes_blob(
        p.codigo, p.codigo_oem, p.codigo_alternativo, p.homologados
    )


def fts_create_table(conn) -> None:
    """Crea las tablas FTS5 si no existen."""
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
    # Tokenizador trigram: permite buscar una subcadena en medio de un código
    # ("180" dentro de "MB891180"), cosa que el índice por palabras no puede.
    conn.execute(
        text(
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS productos_codes_fts USING fts5(
            codigo UNINDEXED,
            codes,
            tokenize='trigram'
        )
    """
        )
    )


def fts_rebuild(conn) -> int:
    """Reconstruye ambos índices completos desde productos activos."""
    fts_create_table(conn)
    conn.execute(text("DELETE FROM productos_fts"))
    conn.execute(text("DELETE FROM productos_codes_fts"))
    rows = conn.execute(
        text(
            """
        SELECT CODIGO, "CODIGO OEM", "CODIGO ALTERNATIVO O ANTIGUO",
               DESCRIPCION, MODELO, MOTOR, MARCA, medidas, HOMOLOGADOS,
               anio, version
        FROM productos WHERE ACTIVO = 1
    """
        )
    ).fetchall()
    if rows:
        conn.execute(
            text("INSERT INTO productos_fts(codigo, blob) VALUES (:c, :b)"),
            [{"c": row[0], "b": _fts_blob(*row)} for row in rows],
        )
        conn.execute(
            text("INSERT INTO productos_codes_fts(codigo, codes) VALUES (:c, :b)"),
            [
                {"c": row[0], "b": _fts_codes_blob(row[0], row[1], row[2], row[8])}
                for row in rows
            ],
        )
    _set_blob_version(conn, FTS_BLOB_VERSION)
    return len(rows)


def _set_blob_version(conn, version: int) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS productos_fts_meta "
            "(clave TEXT PRIMARY KEY, valor TEXT)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO productos_fts_meta(clave, valor) VALUES ('blob_version', :v) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor"
        ),
        {"v": str(version)},
    )


def _get_blob_version(conn) -> int:
    try:
        row = conn.execute(
            text("SELECT valor FROM productos_fts_meta WHERE clave = 'blob_version'")
        ).scalar()
        return int(row) if row is not None else 0
    except Exception:
        return 0


def fts_ensure_current(conn) -> tuple[int, bool]:
    """
    Garantiza que el índice existe, está poblado y usa el formato de blob vigente.

    Devuelve (filas, reconstruido). Se reconstruye si el índice está vacío, si
    quedó desincronizado con `productos`, o si cambió `FTS_BLOB_VERSION`.
    """
    fts_create_table(conn)
    filas = conn.execute(text("SELECT COUNT(*) FROM productos_fts")).scalar() or 0
    codes = conn.execute(text("SELECT COUNT(*) FROM productos_codes_fts")).scalar() or 0
    activos = (
        conn.execute(text("SELECT COUNT(*) FROM productos WHERE ACTIVO = 1")).scalar()
        or 0
    )
    desincronizado = filas != activos or codes != activos
    if filas == 0 or desincronizado or _get_blob_version(conn) != FTS_BLOB_VERSION:
        return fts_rebuild(conn), True
    return filas, False


# El tokenizador trigram necesita al menos 3 caracteres para poder usar el índice.
FTS_TRIGRAM_MIN_LEN = 3


def fts_codes_search(conn, terms: list[str], limit: int) -> list[str] | None:
    """
    Códigos cuyos campos de código contienen todas las subcadenas pedidas.

    Devuelve None si la consulta no es resoluble por trigram (algún término con
    menos de 3 caracteres), para que quien llame use el camino LIKE.
    """
    limpios = [_fts_norm(t) for t in terms if _fts_norm(t)]
    if not limpios:
        return []
    if any(len(t) < FTS_TRIGRAM_MIN_LEN for t in limpios):
        return None
    match = " AND ".join(f'"{_fts_escape_token(t)}"' for t in limpios)
    rows = conn.execute(
        text(
            "SELECT codigo FROM productos_codes_fts WHERE codes MATCH :q "
            "ORDER BY codigo LIMIT :limit"
        ),
        {"q": match, "limit": max(1, int(limit))},
    ).fetchall()
    return [r[0] for r in rows]


def fts_search_codes(conn, match_query: str, limit: int, offset: int = 0) -> list[str]:
    """Códigos ordenados por relevancia, paginados en SQL (no en Python)."""
    q = (match_query or "").strip()
    if not q:
        return []
    rows = conn.execute(
        text(
            "SELECT codigo FROM productos_fts WHERE blob MATCH :q "
            "ORDER BY rank LIMIT :limit OFFSET :offset"
        ),
        {"q": q, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
    ).fetchall()
    return [r[0] for r in rows]


def fts_count(conn, match_query: str) -> int:
    """Total de coincidencias, contado por SQLite sin materializar los códigos."""
    q = (match_query or "").strip()
    if not q:
        return 0
    return int(
        conn.execute(
            text("SELECT COUNT(*) FROM productos_fts WHERE blob MATCH :q"), {"q": q}
        ).scalar()
        or 0
    )


def fts_upsert(conn, codigo: str, blob: str, codes_blob: str | None = None) -> None:
    """Reindexa un producto. `codes_blob` alimenta el índice trigram de códigos."""
    fts_delete(conn, codigo)
    conn.execute(
        text("INSERT INTO productos_fts(codigo, blob) VALUES (:c, :b)"),
        {"c": codigo, "b": blob},
    )
    if codes_blob is not None:
        conn.execute(
            text("INSERT INTO productos_codes_fts(codigo, codes) VALUES (:c, :b)"),
            {"c": codigo, "b": codes_blob},
        )


def fts_upsert_producto(conn, producto) -> None:
    """Reindexa un producto en ambos índices a partir del modelo."""
    fts_upsert(
        conn,
        (producto.codigo or "").strip(),
        fts_blob_de_producto(producto),
        fts_codes_blob_de_producto(producto),
    )


def fts_delete(conn, codigo: str) -> None:
    conn.execute(
        text("DELETE FROM productos_fts WHERE codigo = :c"),
        {"c": codigo},
    )
    conn.execute(
        text("DELETE FROM productos_codes_fts WHERE codigo = :c"),
        {"c": codigo},
    )
