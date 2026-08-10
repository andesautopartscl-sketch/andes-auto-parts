"""
Smoke test de la PWA móvil: recorre sus rutas con sesión simulada.

Comprueba tres cosas que fallan en silencio:
  1. que todas las rutas rendericen 200;
  2. que ningún asset local se sirva sin el parámetro ?v=, porque sin él una
     corrección publicada puede no llegar nunca al dispositivo;
  3. que el service worker precachee exactamente las mismas URLs (con el mismo
     ?v=) que pide la página, o el precache no sirve de nada.

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

fallos: list[tuple[str, str]] = []
sin_version: list[tuple[str, str]] = []
assets: set[str] = set()
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
        # Todo .js/.css local debe llevar cache-buster.
        for ref in re.findall(r'(?:src|href)="(/static/mobile/[^"]+)"', html):
            if ref.split("?")[0].endswith((".js", ".css")):
                assets.add(ref)
                if "?v=" not in ref:
                    sin_version.append((ruta, ref))
        print(f"  {r.status_code}  {ms:7.0f} ms  {len(html):7d} b  {ruta}")

    # Los assets referenciados deben existir de verdad.
    for ref in sorted(assets):
        if c.get(ref).status_code != 200:
            fallos.append((ref, "asset inexistente"))

    # El service worker debe precachear la URL exacta que pide la página: la
    # caché indexa por URL completa, así que un ?v= distinto la vuelve inútil.
    sw = c.get("/m/service-worker.js").get_data(as_text=True)
    version = re.search(r'SW_VERSION = "andes-mobile-v(\d+)"', sw)
    version = version.group(1) if version else "?"
    precache = {
        ruta.replace("${ASSET_V}", version)
        for ruta in re.findall(r"[`\"](/static/mobile/[^`\"]+)", sw)
    }
    for ref in sorted(assets):
        if ref not in precache:
            equivalente = next((p for p in precache if p.split("?")[0] == ref.split("?")[0]), None)
            motivo = (
                f"el service worker precachea {equivalente!r} en su lugar"
                if equivalente
                else "no está en el precache del service worker"
            )
            fallos.append((ref, motivo))

print()
if sin_version:
    print("ASSETS SIN CACHE-BUSTER:")
    for ruta, ref in sin_version:
        print(f"  {ruta} -> {ref}")
if fallos:
    print("FALLOS:")
    for donde, err in fallos:
        print(f"  {donde}: {err}")
    sys.exit(1)
print(
    f"OK: {len(RUTAS)} rutas en 200, {len(assets)} assets versionados "
    f"y alineados con el precache del service worker"
)
