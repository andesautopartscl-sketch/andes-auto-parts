"""
Sincronización de URLs Cloudinary (oem_despiece.imagen_static, productos.despiece).

Usado por scripts/sync_cloudinary_urls.py y GET /admin/sync-cloudinary-urls.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.environ.get("ANDES_DB_PATH") or (PROJECT_ROOT / "data" / "andes.db"))
DEFAULT_SQL = PROJECT_ROOT / "scripts" / "sync_cloudinary_urls.sql"
SQLITE_TIMEOUT = 30

OEM_QUERY = """
    SELECT oem_norm, imagen_static
    FROM oem_despiece
    WHERE imagen_static IS NOT NULL
      AND instr(lower(imagen_static), 'cloudinary') > 0
    ORDER BY oem_norm
"""

PRODUCTOS_QUERY = """
    SELECT CODIGO, despiece
    FROM productos
    WHERE despiece IS NOT NULL
      AND instr(lower(despiece), 'cloudinary') > 0
    ORDER BY CODIGO
"""

_WHERE_LITERAL = re.compile(
    r"WHERE\s+(?:CODIGO|oem_norm)\s*=\s*'((?:''|[^'])*)'",
    re.IGNORECASE,
)


@dataclass
class SyncApplyResult:
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


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def fetch_cloudinary_rows(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    oem_rows = [(str(r[0]), str(r[1])) for r in conn.execute(OEM_QUERY)]
    prod_rows = [(str(r[0]), str(r[1])) for r in conn.execute(PRODUCTOS_QUERY)]
    return oem_rows, prod_rows


def build_sync_sql(oem_rows: list[tuple[str, str]], prod_rows: list[tuple[str, str]]) -> str:
    lines: list[str] = [
        "-- Generado por scripts/sync_cloudinary_urls.py --generate",
        "-- Aplicar: python scripts/sync_cloudinary_urls.py --apply",
        "-- oem_despiece: clave oem_norm (los id locales no coinciden con Render)",
        "BEGIN TRANSACTION;",
        "",
    ]
    if oem_rows:
        lines.append(f"-- oem_despiece ({len(oem_rows)} registros)")
        for oem_norm, url in oem_rows:
            lines.append(
                "UPDATE oem_despiece "
                f"SET imagen_static={sql_literal(url)} "
                f"WHERE oem_norm={sql_literal(oem_norm)};"
            )
        lines.append("")

    if prod_rows:
        lines.append(f"-- productos.despiece ({len(prod_rows)} registros)")
        for codigo, url in prod_rows:
            lines.append(
                "UPDATE productos "
                f"SET despiece={sql_literal(url)} "
                f"WHERE CODIGO={sql_literal(codigo)};"
            )
        lines.append("")

    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def generate_sync_sql_file(db_path: Path | None = None, sql_path: Path | None = None) -> dict:
    db = resolve_db_path(db_path)
    sql = resolve_sql_path(sql_path)
    if not db.is_file():
        raise FileNotFoundError(f"No existe la base de datos: {db}")

    conn = connect_db(db)
    try:
        oem_rows, prod_rows = fetch_cloudinary_rows(conn)
    finally:
        conn.close()

    sql.write_text(build_sync_sql(oem_rows, prod_rows), encoding="utf-8")
    total = len(oem_rows) + len(prod_rows)
    return {
        "db_path": str(db),
        "sql_path": str(sql),
        "oem_despiece": len(oem_rows),
        "productos": len(prod_rows),
        "total": total,
    }


def iter_update_statements(sql_path: Path) -> list[str]:
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
        if upper.startswith("UPDATE "):
            statements.append(line if line.endswith(";") else f"{line};")
    return statements


def _unescape_sql_literal(value: str) -> str:
    return value.replace("''", "'")


def _stmt_target_label(stmt: str) -> str:
    upper = stmt.upper()
    m = _WHERE_LITERAL.search(stmt)
    key = _unescape_sql_literal(m.group(1)) if m else "?"
    if "UPDATE PRODUCTOS" in upper:
        return f"productos CODIGO={key}"
    if "UPDATE OEM_DESPIECE" in upper:
        return f"oem_despiece oem_norm={key}"
    return "desconocido"


def apply_cloudinary_url_sync(
    db_path: Path | None = None,
    sql_path: Path | None = None,
    *,
    dry_run: bool = False,
) -> SyncApplyResult:
    db = resolve_db_path(db_path)
    sql = resolve_sql_path(sql_path)
    result = SyncApplyResult(ok=False, db_path=str(db), sql_path=str(sql))

    if not db.is_file():
        result.error = f"No existe la base de datos: {db}"
        return result

    try:
        statements = iter_update_statements(sql)
    except FileNotFoundError:
        result.error = f"No existe {sql}. Ejecute --generate en local primero."
        return result

    if not statements:
        result.error = "No hay sentencias UPDATE en el archivo SQL."
        return result

    result.total = len(statements)
    conn = connect_db(db)

    try:
        if not dry_run:
            conn.execute("BEGIN")

        for stmt in statements:
            label = _stmt_target_label(stmt)
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
