"""Lógica compartida: códigos de catálogo y coincidencia de nombres de archivo (Pics vs BD)."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import ahocorasick

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "andes.db"

DEFAULT_PICS = Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop" / "Pics"

IMG_EXT = {".jpg", ".jpeg", ".png"}


def _homologado_tokens(raw: str | None) -> list[str]:
    s = (raw or "").replace("\xa0", " ").replace("\u2007", " ").strip()
    if not s:
        return []
    parts = re.split(r"[\s,;/|]+", s)
    out: list[str] = []
    for t in parts:
        u = t.strip().upper()
        if not u or u in ("/", "-", "|", "."):
            continue
        out.append(u)
    return out


def _split_codigos_alternativos(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[/;,|\n]+", str(raw)) if p.strip()]


def _token_ok(token: str) -> bool:
    t = (token or "").strip().upper()
    if len(t) < 3:
        return False
    if len(t) == 1 and t.isdigit():
        return False
    return True


def load_all_codes(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(
        """
        SELECT [CODIGO], [CODIGO OEM], [CODIGO ALTERNATIVO O ANTIGUO], [HOMOLOGADOS]
        FROM productos
        WHERE COALESCE([ACTIVO], 1) = 1
        """
    )
    codes: set[str] = set()
    for row in cur.fetchall():
        codigo, oem, alt, hom = row
        if codigo and str(codigo).strip():
            c = str(codigo).strip().upper()
            if _token_ok(c):
                codes.add(c)
        if oem:
            s_oem = str(oem)
            for t in _homologado_tokens(s_oem):
                if _token_ok(t):
                    codes.add(t)
            whole = s_oem.strip().upper()
            if len(whole) >= 3:
                codes.add(whole)
        if alt:
            for t in _split_codigos_alternativos(str(alt)):
                u = t.strip().upper()
                if _token_ok(u):
                    codes.add(u)
        if hom:
            for t in _homologado_tokens(str(hom)):
                if _token_ok(t):
                    codes.add(t)
    return codes


def build_automatons(codes: set[str]) -> tuple[ahocorasick.Automaton, ahocorasick.Automaton]:
    a_sub = ahocorasick.Automaton()
    a_compact = ahocorasick.Automaton()
    seen_sub: set[str] = set()
    seen_c: set[str] = set()
    for c in codes:
        cl = c.lower()
        if len(cl) >= 3 and cl not in seen_sub:
            seen_sub.add(cl)
            a_sub.add_word(cl, cl)
        compact_needle = "".join(cl.split())
        if len(compact_needle) >= 3 and compact_needle not in seen_c:
            seen_c.add(compact_needle)
            a_compact.add_word(compact_needle, compact_needle)
    a_sub.make_automaton()
    a_compact.make_automaton()
    return a_sub, a_compact


def file_matches(name_lower: str, a_sub: ahocorasick.Automaton, a_compact: ahocorasick.Automaton) -> bool:
    for _end, _val in a_sub.iter(name_lower):
        return True
    compact_name = name_lower.replace(" ", "").replace("_", "")
    for _end, _val in a_compact.iter(compact_name):
        return True
    return False
