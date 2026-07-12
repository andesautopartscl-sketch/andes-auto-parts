# Andes Mobile

App móvil (PWA) **separada** del ERP. En Cursor ábrela sola:

- `C:\App movil andes` (atajo local), o
- `AndesAutoParts/andes_mobile` (misma carpeta en el repo)

## Estructura

```
templates/mobile/   HTML de /m/
static/             CSS, JS, icons, PWA
server/             Python del blueprint /m/
README.md
```

## Cómo se conecta al ERP

El ERP solo tiene un puente mínimo (`app/mobile/__init__.py` + utils de path/login).
En producción (Render) usa esta carpeta dentro del repo (`andes_mobile/`).

Variable opcional: `ANDES_MOBILE_ROOT`.

## Qué editar dónde

| Cambio | Dónde |
|--------|--------|
| UI, PWA, scanner, estilos | aquí (`templates/`, `static/`) |
| Rutas/API `/m/` | aquí (`server/`) |
| Bodega, ventas, DB, ERP | `AndesAutoParts` (otra ventana de Cursor) |

El servidor del ERP debe estar corriendo para probar `/m/`.
