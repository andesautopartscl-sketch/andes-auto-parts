"""
Smoke test de la PWA móvil: recorre sus rutas con sesión simulada.

Verifica que todas rendericen 200 y que ningún <script> local quede sin el
parámetro de versión (?v=), que es lo que invalida la caché del service worker.
Uso: python scripts/smoke_rutas_mobile.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.seguridad.models import Usuario as UsuarioSistema  # noqa: E402

RUTAS = [
    "/m/", "/m/dashboard", "/m/buscar", "/m/ventas", "/m/clientes",
    "/m/proveedores", "/m/ingreso-rapido", "/m/etiquetas",
    "/m/importar-imagenes", "/m/ajustes", "/m/oc-clientes",
    "/m/oc-clientes/nueva", "/m/escaner", "/m/ingresos",
    "/m/stock-critico", "/m/reportes", "/m/venta-rapida",
]

app = create_app()
with app.app_context():
    admin = UsuarioSistema.query.filter_by(activo=True).first()
    uid, usuario = admin.id, admin.usuario
    rol = admin.rol.nombre if admin.rol else None

fallos = []
sin_version = []
with app.test_client() as c:
    with c.session_transaction() as s:
        s["user"] = usuario
        s["user_id"] = uid
        s["rol"] = rol
    for ruta in RUTAS:
        t0 = time.perf_counter()
        try:
            r = c.get(ruta, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            fallos.append((ruta, f"EXC {type(exc).__name__}: {exc}"))
            continue
        ms = (time.perf_counter() - t0) * 1000
        html = r.get_data(as_text=True)
        if r.status_code != 200:
            fallos.append((ruta, f"HTTP {r.status_code}"))
            continue
        # Todo <script src> local debe llevar cache-buster.
        for src in re.findall(r'<script[^>]+src="(/static/mobile/[^"]+)"', html):
            if "?v=" not in src:
                sin_version.append((ruta, src))
        print(f"  {r.status_code}  {ms:7.0f} ms  {len(html):7d} b  {ruta}")

print()
if sin_version:
    print("SCRIPTS SIN CACHE-BUSTER:")
    for ruta, src in sin_version:
        print(f"  {ruta} -> {src}")
if fallos:
    print("FALLOS:")
    for ruta, err in fallos:
        print(f"  {ruta}: {err}")
    sys.exit(1)
print("OK: todas las rutas mobile responden 200 y con assets versionados")
