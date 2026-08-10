"""
Smoke test del ERP: renderiza todas las rutas GET con sesión simulada.

Detecta plantillas rotas, errores 500 y rutas lentas. Vuelca además el JS inline
generado para poder validarlo con `node --check`. Uso: python scripts/smoke_rutas_erp.py
"""
from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import create_app  # noqa: E402
from app.seguridad.models import Usuario  # noqa: E402

SALIDA = RAIZ / "_js_render"
SCRIPT_RE = re.compile(
    r'<script(?![^>]*\bsrc=)(?![^>]*type="application/(?:ld\+)?json")[^>]*>(.*?)</script>',
    re.S | re.I,
)

app = create_app()

with app.app_context():
    admin = Usuario.query.filter_by(activo=True).first()
    uid, usuario = admin.id, admin.usuario
    rol = admin.rol.nombre if admin.rol else None

# Rutas GET sin parametros obligatorios.
rutas = []
for regla in app.url_map.iter_rules():
    if "GET" not in (regla.methods or set()) or regla.arguments:
        continue
    ruta = str(regla.rule)
    if ruta.startswith(("/static", "/m/")) or "logout" in ruta:
        continue
    rutas.append(ruta)
rutas = sorted(set(rutas))

shutil.rmtree(SALIDA, ignore_errors=True)
SALIDA.mkdir(parents=True, exist_ok=True)

fallos, lentas, ok = [], [], 0
with app.test_client() as c:
    with c.session_transaction() as s:
        s["user"] = usuario
        s["user_id"] = uid
        s["rol"] = rol
    for ruta in rutas:
        t0 = time.perf_counter()
        try:
            r = c.get(ruta, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            fallos.append((ruta, f"{type(exc).__name__}: {exc}"))
            continue
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 500:
            fallos.append((ruta, f"HTTP {r.status_code}"))
            continue
        ok += 1
        if ms > 800:
            lentas.append((ms, ruta))
        if "text/html" not in (r.content_type or ""):
            continue
        html = r.get_data().decode("utf-8", "replace")
        nombre = ruta.strip("/").replace("/", "_").replace("<", "").replace(">", "") or "index"
        for i, cuerpo in enumerate(SCRIPT_RE.findall(html)):
            if cuerpo.strip():
                (SALIDA / f"{nombre}__{i}.js").write_text(cuerpo, encoding="utf-8")

print(f"rutas OK: {ok}/{len(rutas)}")
if lentas:
    print("\nRutas lentas (>800 ms):")
    for ms, ruta in sorted(lentas, reverse=True)[:15]:
        print(f"  {ms:8.0f} ms  {ruta}")
if fallos:
    print("\nFALLOS:")
    for ruta, err in fallos:
        print(f"  {ruta}: {err}")
print(f"\nbloques JS volcados en {SALIDA}: {len(list(SALIDA.glob('*.js')))}")
sys.exit(1 if fallos else 0)
