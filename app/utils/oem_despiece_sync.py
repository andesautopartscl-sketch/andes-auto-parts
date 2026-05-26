"""
Exportar / aplicar filas oem_despiece (INSERT OR REPLACE) para sincronizar local → Render.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.cloudinary_url_sync import sql_literal

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.environ.get("ANDES_DB_PATH") or (PROJECT_ROOT / "data" / "andes.db"))
DEFAULT_SQL = PROJECT_ROOT / "scripts" / "sync_oem_despiece.sql"
SQLITE_TIMEOUT = 30

# Registros con URL Cloudinary que faltaban en Render (oem_norm reales en BD local).
DEFAULT_OEM_NORMS: tuple[str, ...] = (
    "10046260",
    "C00038336",
    "SX5-2906013",
    "_INT_VG52405",
    "_INT_VG52406",
    "_INT_VGP3092",
    "_INT_VGP3093",
    "C00015176",
)

INSERT_COLUMNS = (
    "oem_norm",
    "producto_codigo",
    "titulo",
    "imagen_static",
    "partes_json",
    "notas",
    "updated_at",
)


@dataclass
class OemDespieceSyncResult:
    ok: bool
    actualizados: int = 0
    omitidos: int = 0
    errores: int = 0
    total: int = 0
    detalles: list[str] = field(default_factory=list)
    error: str | None = None
    db_path: str = ""
    sql_path: str = ""

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "actualizados": self.actualizados,
            "omitidos": self.omitidos,
            "errores": self.errores,
            "total": self.total,
            "detalles": self.detalles,
            "db_path": self.db_path,
            "sql_path": self.sql_path,
        }
        if self.error:
            out["error"] = self.error
        return out


def connect_db(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT)


def resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path.resolve()
    return DEFAULT_DB.resolve()


def resolve_sql_path(sql_path: Path | None = None) -> Path:
    if sql_path is not None:
        return sql_path.resolve()
    return DEFAULT_SQL.resolve()


def _sql_value(value) -> str:
    if value is None:
        return "NULL"
    return sql_literal(str(value))


def fetch_oem_despiece_rows(
    conn: sqlite3.Connection,
    oem_norms: tuple[str, ...] | list[str],
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if not oem_norms:
        return []
    placeholders = ",".join("?" for _ in oem_norms)
    query = f"""
        SELECT id, oem_norm, producto_codigo, titulo, imagen_static,
               partes_json, notas, updated_at
        FROM oem_despiece
        WHERE oem_norm IN ({placeholders})
        ORDER BY oem_norm
    """
    return list(conn.execute(query, tuple(oem_norms)))


def build_insert_sql(rows: list[sqlite3.Row]) -> str:
    lines: list[str] = [
        "-- Generado por scripts/sync_oem_despiece.py --generate",
        "-- Aplicar: python scripts/sync_oem_despiece.py --apply",
        "BEGIN TRANSACTION;",
        "",
    ]
    cols = ", ".join(INSERT_COLUMNS)
    for row in rows:
        values = ", ".join(_sql_value(row[c]) for c in INSERT_COLUMNS)
        oem = row["oem_norm"]
        lines.append(f"-- oem_norm={oem}")
        lines.append(
            f"INSERT OR REPLACE INTO oem_despiece ({cols}) VALUES ({values});"
        )
        lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def generate_oem_despiece_sql_file(
    oem_norms: tuple[str, ...] | list[str] | None = None,
    db_path: Path | None = None,
    sql_path: Path | None = None,
) -> dict:
    norms = tuple(oem_norms) if oem_norms is not None else DEFAULT_OEM_NORMS
    db = resolve_db_path(db_path)
    sql = resolve_sql_path(sql_path)
    if not db.is_file():
        raise FileNotFoundError(f"No existe la base de datos: {db}")

    conn = connect_db(db)
    try:
        rows = fetch_oem_despiece_rows(conn, norms)
    finally:
        conn.close()

    found = {r["oem_norm"] for r in rows}
    missing = [n for n in norms if n not in found]

    sql.write_text(build_insert_sql(rows), encoding="utf-8")
    return {
        "db_path": str(db),
        "sql_path": str(sql),
        "solicitados": len(norms),
        "encontrados": len(rows),
        "omitidos_generacion": missing,
        "oem_norms": [r["oem_norm"] for r in rows],
    }


def iter_insert_statements(sql_path: Path) -> list[str]:
    if not sql_path.is_file():
        raise FileNotFoundError(sql_path)
    statements: list[str] = []
    for raw in sql_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        upper = line.upper()
        if upper in ("BEGIN TRANSACTION;", "COMMIT;", "BEGIN;", "COMMIT;"):
            continue
        if upper.startswith("INSERT OR REPLACE INTO OEM_DESPIECE"):
            statements.append(line if line.endswith(";") else f"{line};")
    return statements


def _insert_oem_norm(stmt: str) -> str:
    marker = "oem_norm"
    idx = stmt.lower().find(marker)
    if idx < 0:
        return "?"
    rest = stmt[idx:]
    eq = rest.find("=")
    if eq < 0:
        return "?"
    start = rest.find("'", eq)
    if start < 0:
        return "?"
    start += 1
    parts: list[str] = []
    i = start
    while i < len(rest):
        ch = rest[i]
        if ch == "'":
            if i + 1 < len(rest) and rest[i + 1] == "'":
                parts.append("'")
                i += 2
                continue
            break
        parts.append(ch)
        i += 1
    return "".join(parts)


def apply_oem_despiece_sql(
    db_path: Path | None = None,
    sql_path: Path | None = None,
    *,
    dry_run: bool = False,
) -> OemDespieceSyncResult:
    db = resolve_db_path(db_path)
    sql = resolve_sql_path(sql_path)
    result = OemDespieceSyncResult(ok=False, db_path=str(db), sql_path=str(sql))

    if not db.is_file():
        result.error = f"No existe la base de datos: {db}"
        return result

    try:
        statements = iter_insert_statements(sql)
    except FileNotFoundError:
        result.error = f"No existe {sql}. Ejecute --generate en local primero."
        return result

    if not statements:
        result.error = "No hay sentencias INSERT en el archivo SQL."
        return result

    result.total = len(statements)
    conn = connect_db(db)

    try:
        if not dry_run:
            conn.execute("BEGIN")

        for stmt in statements:
            oem = _insert_oem_norm(stmt)
            label = f"oem_despiece oem_norm={oem}"
            if dry_run:
                result.actualizados += 1
                result.detalles.append(f"DRY-RUN {label}")
                continue

            try:
                cur = conn.execute(stmt)
                n = cur.rowcount
            except sqlite3.Error as exc:
                result.errores = 1
                result.error = str(exc)
                result.detalles.append(f"ERROR {label}: {exc}")
                if not dry_run:
                    conn.rollback()
                return result

            if n > 0:
                result.actualizados += 1
                result.detalles.append(f"OK {label}")
            else:
                result.omitidos += 1
                result.detalles.append(f"SIN CAMBIO {label}")

        if not dry_run:
            conn.commit()
    except Exception as exc:
        if not dry_run:
            conn.rollback()
        result.error = str(exc)
        result.errores = 1
        return result
    finally:
        conn.close()

    result.ok = result.errores == 0 and result.omitidos == 0
    return result
