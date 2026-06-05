# UptimeRobot — mantener Render despierto (Andes Mobile)

Render en el plan gratuito **se duerme** tras ~15 minutos sin tráfico. El primer request después puede tardar **30–60 segundos** (cold start). UptimeRobot hace ping periódico a la PWA para reducir esos despertares.

## URL a monitorear

```
https://andes-auto-parts.onrender.com/m/
```

Usa `/m/` (home mobile): responde 302 a login si no hay sesión, pero **sigue contando como sitio activo** para Render.

## Paso a paso (plan gratuito)

### 1. Crear cuenta

1. Ir a [https://uptimerobot.com](https://uptimerobot.com)
2. **Register** con email o Google
3. Confirmar el correo si lo pide

### 2. Nuevo monitor HTTP(s)

1. Dashboard → **+ Add New Monitor**
2. **Monitor Type:** `HTTP(s)`
3. **Friendly Name:** `Andes Auto Parts — PWA /m/`
4. **URL:** `https://andes-auto-parts.onrender.com/m/`
5. **Monitoring Interval:** `5 minutes` (máximo en plan free)
6. **Monitor Timeout:** `30 seconds` (cold start Render puede ser lento la primera vez)
7. Dejar **HTTP Method** en `GET`
8. **Alert Contacts:** agregar tu email (opcional pero recomendado)

### 3. Guardar y verificar

1. Clic en **Create Monitor**
2. En 5–10 minutos el estado debería pasar a **Up** (código 200 o 302)
3. Si queda **Down**:
   - Comprobar que la URL abre en el navegador
   - Revisar que el servicio en Render esté **Live**
   - Subir timeout a 60 s si el cold start es muy largo

### 4. Opcional — keyword monitoring

Si quieres confirmar que Flask responde HTML y no un error de proxy:

1. Editar monitor → **Advanced Settings**
2. **Keyword Monitoring:** `Andes` o `login`
3. Guardar

### 5. Límites del plan free

| Recurso | Límite |
|---------|--------|
| Monitores | 50 |
| Intervalo mínimo | 5 minutos |
| Alertas | Email básico |

Con ping cada 5 min ≈ **288 requests/día** — suficiente para mantener la instancia activa la mayor parte del tiempo.

## Buenas prácticas

- **No** monitorear endpoints POST ni APIs que modifiquen datos
- **Sí** monitorear `/m/` o `/login` (GET ligero)
- Revisar logs en Render si UptimeRobot marca Down intermitente (deploy o migración DB)
- Si migras de dominio, actualizar la URL del monitor

## Alternativa sin UptimeRobot

Cron-job externo (cron-job.org, GitHub Actions schedule) con `curl -I https://andes-auto-parts.onrender.com/m/` cada 5 min — mismo efecto, más manual de mantener.

## Verificación manual

```bash
curl -I https://andes-auto-parts.onrender.com/m/
```

Respuesta esperada: `HTTP/2 302` (redirect a login) o `200` con sesión — ambos indican que el servicio está vivo.
