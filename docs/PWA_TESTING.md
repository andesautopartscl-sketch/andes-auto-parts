# Checklist PWA — Samsung Galaxy S24 Ultra

Pruebas de la PWA Andes Mobile en **Chrome Android** contra producción o staging con HTTPS.

**URL base:** `https://andes-auto-parts.onrender.com/m/`

---

## Instalación y shell

- [ ] Instalación desde Chrome (menú ⋮ → *Instalar aplicación* o banner *Añadir a pantalla de inicio*)
- [ ] Ícono aparece en home (letra **A** navy `#1B2B6B`)
- [ ] Abre en modo **standalone** (sin barra de direcciones del navegador)
- [ ] Splash Andes Auto Parts visible 1–2 s al abrir desde el ícono
- [ ] Barra de estado respeta `theme-color` navy

---

## Sesión y navegación

- [ ] Login funciona (redirect desde `/m/` → login → vuelve a mobile)
- [ ] Bottom nav: Inicio, Buscar, Escáner, Ventas, Más
- [ ] Menú *Más*: Dashboard, Ingresos, Stock crítico, Reportes

---

## Datos y búsqueda

- [ ] Dashboard carga datos reales (ventas/ingresos hoy, stock crítico)
- [ ] Búsqueda con debounce (~300 ms) en `/m/buscar`
- [ ] Ficha producto muestra precio, stock por bodega, imagen (lazy load)
- [ ] Skeleton “Sincronizando catálogo…” aparece brevemente tras login (si hay red)

---

## Escáner

- [ ] Escáner QR funciona (probar con etiqueta impresa del ERP)
- [ ] Escáner barcode funciona (modo stock)
- [ ] Modo venta: escaneo agrega producto al wizard venta rápida
- [ ] Linterna / cambiar cámara (si el dispositivo lo soporta)

---

## Venta rápida (requiere internet)

- [ ] Crear venta completa: cliente + productos + método de pago
- [ ] La venta aparece en el ERP web con tag **`[Andes Mobile]`** en observaciones
- [ ] Boleta/factura con número correlativo correcto
- [ ] Consumidor final / cliente sin registrar (si aplica permisos)

---

## Stock

- [ ] Ajustar stock funciona (ingreso/salida/ajuste con motivo)
- [ ] Movimiento visible en ERP / historial bodega

---

## Modo offline

- [ ] Activar **Modo avión** o DevTools → Network → Offline (ver abajo)
- [ ] Banner amarillo: *Sin conexión — mostrando datos cacheados*
- [ ] Búsqueda devuelve resultados del catálogo IndexedDB
- [ ] Botones venta rápida y ajustar stock **deshabilitados** (tooltip *Requiere internet*)
- [ ] Lectura de fichas ya visitadas / HTML cacheado sigue navegable

---

## Infraestructura Render

- [ ] Cold start de Render **< 60 s** después de 15+ min inactividad (con UptimeRobot configurado)
- [ ] UptimeRobot monitor en **Up** (ver `docs/UPTIMEROBOT.md`)

---

## Lighthouse (opcional en desktop)

Chrome DevTools → Lighthouse → categoría **Progressive Web App** en URL `/m/` (logueado):

- [ ] Score PWA **> 90**
- [ ] Manifest válido, SW registrado, HTTPS, viewport

---

## Simular offline en localhost

1. `flask run` o servidor local con HTTPS (SW requiere secure context; localhost es excepción)
2. Login en `http://127.0.0.1:5000/m/`
3. Esperar sincronización de catálogo (~barra de progreso)
4. F12 → **Network** → checkbox **Offline**
5. Ir a **Buscar** y probar término conocido
6. Confirmar banner offline y botones deshabilitados en venta rápida

---

## Instalación en S24 Ultra (resumen)

1. Chrome → `https://andes-auto-parts.onrender.com/m/`
2. Iniciar sesión
3. Menú **⋮** → **Añadir a pantalla de inicio** / **Instalar aplicación**
4. Confirmar nombre **Andes** e ícono
5. Abrir desde el launcher — debe abrir standalone con splash

---

## Reporte de incidencias

Anotar: fecha, build/commit, paso fallido, captura, ¿online u offline?, usuario/rol.
