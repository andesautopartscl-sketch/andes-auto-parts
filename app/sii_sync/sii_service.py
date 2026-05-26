"""Cliente HTTP para consultar RCV (ventas) y datos de contribuyentes vía API intermediaria."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

from app.utils.rut_utils import clean_rut, format_rut, is_valid_rut

logger = logging.getLogger(__name__)

DTE_TIPOS_VALIDOS = {"33", "34", "39", "41", "52", "56", "61"}
ESTADOS_VALIDOS = {"ACEPTADO", "RECHAZADO", "PENDIENTE"}

_CONTRIBUYENTE_CACHE: dict[str, dict[str, Any]] = {}
_CONTRIBUYENTE_CACHE_MAX = 500
_CONTRIBUYENTE_CACHE_TTL_SEC = 24 * 60 * 60


def _contribuyente_cache_key(rut: str) -> str:
    return clean_rut(rut).upper()


def _contribuyente_cache_get(rut: str) -> dict[str, str] | None:
    key = _contribuyente_cache_key(rut)
    if not key:
        return None
    entry = _CONTRIBUYENTE_CACHE.get(key)
    if not entry:
        return None
    if time.time() - float(entry.get("timestamp") or 0) > _CONTRIBUYENTE_CACHE_TTL_SEC:
        _CONTRIBUYENTE_CACHE.pop(key, None)
        return None
    datos = entry.get("datos")
    if isinstance(datos, dict):
        return dict(datos)
    return None


def _contribuyente_cache_set(rut: str, datos: dict[str, str]) -> None:
    key = _contribuyente_cache_key(rut)
    if not key:
        return
    _CONTRIBUYENTE_CACHE[key] = {"datos": dict(datos), "timestamp": time.time()}
    while len(_CONTRIBUYENTE_CACHE) > _CONTRIBUYENTE_CACHE_MAX:
        oldest_key = min(
            _CONTRIBUYENTE_CACHE,
            key=lambda k: float(_CONTRIBUYENTE_CACHE[k].get("timestamp") or 0),
        )
        _CONTRIBUYENTE_CACHE.pop(oldest_key, None)


class SIIServiceError(Exception):
    pass


class ContribuyenteNotFoundError(Exception):
    pass


class SIIService:
    BASEAPI_URL = "https://api.baseapi.cl"

    def __init__(self) -> None:
        from app.utils.load_env import load_project_dotenv

        load_project_dotenv(force=True)
        self._reload_from_environ()
        self._contribuyente_cache_hit = False

    def _reload_from_environ(self) -> None:
        """Lee variables desde os.environ (tras recargar .env)."""
        self.provider = (os.environ.get("SII_API_PROVIDER") or "baseapi").strip().lower()
        self.api_key = (os.environ.get("SII_API_KEY") or "").strip()
        self.rut_empresa = (os.environ.get("SII_RUT_EMPRESA") or "").strip()
        self.rut_auth = (os.environ.get("SII_RUT") or self.rut_empresa).strip()
        self.sii_password = (os.environ.get("SII_PASSWORD") or "").strip()
        self.ambiente = (os.environ.get("SII_AMBIENTE") or "certificacion").strip().lower()
        self.timeout = 15
        self.contribuyente_timeout = 5
        self.max_retries = 2
        self.contribuyente_max_retries = 0

    def missing_config_labels(self) -> list[str]:
        missing: list[str] = []
        if not self.api_key:
            missing.append("SII_API_KEY")
        if not self.rut_empresa:
            missing.append("SII_RUT_EMPRESA")
        if self.provider == "baseapi" and not self.sii_password:
            missing.append("SII_PASSWORD")
        return missing

    def configured(self) -> bool:
        return not self.missing_config_labels()

    def contribuyente_lookup_ready(self) -> bool:
        """Consulta pública de situación tributaria solo requiere API key."""
        return bool(self.api_key)

    def _can_use_datos_receptor(self) -> bool:
        return bool(self.api_key and self.sii_password and self.rut_auth)

    def consultar_contribuyente(self, rut: str) -> dict[str, str]:
        """
        Datos para autocompletar cliente/proveedor.
        Orden: situacion-tributaria (público, personas naturales) y luego datos-receptor.
        Docs BaseAPI: situacion-tributaria, datos-receptor (no hay endpoint distinto PN vs empresa).
        """
        if not self.contribuyente_lookup_ready():
            raise SIIServiceError("SII no configurado: falta SII_API_KEY")

        rut_fmt = format_rut(rut)
        if not rut_fmt or not is_valid_rut(rut_fmt):
            raise SIIServiceError("RUT inválido")

        self._contribuyente_cache_hit = False
        cached = _contribuyente_cache_get(rut_fmt)
        if cached:
            logger.debug("contribuyente cache HIT rut=%s", rut_fmt)
            self._contribuyente_cache_hit = True
            return cached

        persona_natural = self._is_persona_natural(rut_fmt)
        logger.info(
            "consultar_contribuyente rut=%s persona_natural=%s",
            rut_fmt,
            persona_natural,
        )

        merged: dict[str, str] = {
            "razon_social": "",
            "giro": "",
            "direccion": "",
            "comuna": "",
            "region": "",
            "estado_sii": "",
        }

        # 1) Situación tributaria primero (rápido; funciona para persona natural)
        merged = self._try_situacion_tributaria(rut_fmt, merged)

        # 2) Datos receptor (emisión DTE; suele fallar o demorar en PN sin datos de factura)
        if self._should_try_datos_receptor(rut_fmt, merged, persona_natural):
            merged = self._try_datos_receptor(rut_fmt, merged)

        merged["region"] = self._resolve_region(
            merged.get("comuna", ""),
            merged.get("ciudad", ""),
            merged.get("region", ""),
        )
        merged.pop("ciudad", None)
        result = self._finalize_contribuyente_result(merged, rut_fmt)
        _contribuyente_cache_set(rut_fmt, result)
        return result

    @staticmethod
    def _is_masked_sii_text(value: str | None) -> bool:
        """Nombre/giro oculto por SII (ej. '** **') o vacío."""
        text = (value or "").strip()
        if not text:
            return True
        return not re.sub(r"[\s\*]+", "", text)

    @staticmethod
    def _sanitize_sii_text(value: str | None) -> str:
        if SIIService._is_masked_sii_text(value):
            return ""
        return (value or "").strip()

    @staticmethod
    def _mark_rut_valido_sii(merged: dict[str, str]) -> None:
        merged["_rut_valido_sii"] = "1"

    def _finalize_contribuyente_result(
        self, merged: dict[str, str], rut_fmt: str
    ) -> dict[str, Any]:
        rut_valido = merged.pop("_rut_valido_sii", "") == "1"
        razon = self._sanitize_sii_text(merged.get("razon_social"))
        giro = self._sanitize_sii_text(merged.get("giro"))

        if not razon and not rut_valido:
            logger.warning(
                "contribuyente no encontrado rut=%s merged=%s",
                rut_fmt,
                merged,
            )
            raise ContribuyenteNotFoundError("RUT no encontrado")

        nombre_privado = rut_valido and not razon
        return {
            "razon_social": razon,
            "giro": giro,
            "direccion": (merged.get("direccion") or "").strip(),
            "comuna": (merged.get("comuna") or "").strip(),
            "region": (merged.get("region") or "").strip(),
            "estado_sii": (merged.get("estado_sii") or "").strip(),
            "rut_valido_sii": rut_valido,
            "nombre_privado_sii": nombre_privado,
        }

    @staticmethod
    def _is_persona_natural(rut_fmt: str) -> bool:
        """RUT cuerpo < 50.000.000 → persona natural (convención Chile)."""
        from app.utils.rut_utils import clean_rut

        compact = clean_rut(rut_fmt)
        if len(compact) < 2:
            return False
        body = compact[:-1]
        if not body.isdigit():
            return False
        return int(body) < 50_000_000

    @staticmethod
    def _rut_api_variants(rut_fmt: str) -> list[str]:
        """Formatos de RUT a probar en BaseAPI (con y sin puntos)."""
        from app.utils.rut_utils import clean_rut

        compact = clean_rut(rut_fmt)
        variants: list[str] = []
        if len(compact) >= 2:
            body, dv = compact[:-1], compact[-1]
            plain = f"{body}-{dv}"
            dotted = format_rut(compact)
            for val in (plain, dotted, rut_fmt.strip()):
                if val and val not in variants:
                    variants.append(val)
        elif rut_fmt.strip():
            variants.append(rut_fmt.strip())
        return variants

    def _should_try_datos_receptor(
        self, rut_fmt: str, merged: dict[str, str], persona_natural: bool
    ) -> bool:
        if not self._can_use_datos_receptor():
            return False
        if persona_natural and (merged.get("razon_social") or "").strip():
            # PN: situación tributaria alcanza; datos-receptor suele demorar/fallar
            return False
        if not (merged.get("razon_social") or "").strip():
            return True
        if not (merged.get("direccion") or "").strip():
            return True
        return False

    def _try_situacion_tributaria(self, rut_fmt: str, merged: dict[str, str]) -> dict[str, str]:
        last_error: Exception | None = None
        for rut_var in self._rut_api_variants(rut_fmt):
            try:
                situacion = self._fetch_situacion_tributaria(rut_var)
                self._log_contribuyente_response("situacion-tributaria", rut_var, situacion)
                patch = self._normalize_situacion_tributaria(situacion)
                merged = self._merge_contribuyente(merged, patch)
                self._mark_rut_valido_sii(merged)
                if (merged.get("razon_social") or "").strip():
                    return merged
            except SIIServiceError as exc:
                last_error = exc
                logger.warning(
                    "situacion-tributaria rut=%s error=%s", rut_var, exc
                )
        if last_error and not (merged.get("razon_social") or "").strip():
            logger.warning(
                "situacion-tributaria agotada para %s: %s", rut_fmt, last_error
            )
        return merged

    def _try_datos_receptor(self, rut_fmt: str, merged: dict[str, str]) -> dict[str, str]:
        last_error: Exception | None = None
        for rut_var in self._rut_api_variants(rut_fmt):
            try:
                raw_dr = self._fetch_datos_receptor(rut_var)
                self._log_contribuyente_response("datos-receptor", rut_var, raw_dr)
                merged = self._merge_contribuyente(
                    merged, self._normalize_datos_receptor(raw_dr)
                )
                self._mark_rut_valido_sii(merged)
                if (merged.get("razon_social") or "").strip():
                    return merged
            except SIIServiceError as exc:
                last_error = exc
                logger.info("datos-receptor rut_receptor=%s error=%s", rut_var, exc)
        if last_error:
            logger.info("datos-receptor agotado para %s: %s", rut_fmt, last_error)
        return merged

    def _log_contribuyente_response(
        self, endpoint: str, rut: str, data: dict[str, Any]
    ) -> None:
        try:
            serialized = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = str(data)
        logger.info(
            "BaseAPI %s rut=%s respuesta_completa=%s",
            endpoint,
            rut,
            serialized[:8000],
        )

    def _fetch_contribuyente_post(
        self, url: str, body: dict[str, Any], endpoint: str, rut_label: str
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        attempts = self.contribuyente_max_retries + 1
        for attempt in range(attempts):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=self.contribuyente_timeout,
                )
                payload = self._parse_http_response(resp)
                self._log_contribuyente_response(
                    f"{endpoint}_raw", rut_label, payload if isinstance(payload, dict) else {"raw": payload}
                )
                return self._unwrap_data(payload)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "BaseAPI %s intento %s/%s rut=%s: %s",
                    endpoint,
                    attempt + 1,
                    attempts,
                    rut_label,
                    exc,
                )
                if attempt < attempts - 1:
                    time.sleep(0.3)
        raise SIIServiceError(
            f"Error de conexión BaseAPI ({endpoint}): {last_error}"
        ) from last_error

    def _fetch_datos_receptor(self, rut_receptor: str) -> dict[str, Any]:
        url = f"{self.BASEAPI_URL}/api/v1/sii/contribuyente/datos-receptor"
        body: dict[str, str] = {
            "rut": self.rut_auth,
            "password": self.sii_password,
            "rut_receptor": rut_receptor,
        }
        if self.rut_empresa:
            body["rut_empresa"] = self.rut_empresa
        return self._fetch_contribuyente_post(
            url, body, "datos-receptor", rut_receptor
        )

    def _fetch_situacion_tributaria(self, rut: str) -> dict[str, Any]:
        url = f"{self.BASEAPI_URL}/api/v1/sii/contribuyente/situacion-tributaria"
        return self._fetch_contribuyente_post(
            url, {"rut": rut}, "situacion-tributaria", rut
        )

    @staticmethod
    def _unwrap_data(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and payload.get("success") is False:
            msg = payload.get("message") or payload.get("error") or "Error en API BaseAPI"
            raise SIIServiceError(str(msg))
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                if data.get("contribuyente") and isinstance(data["contribuyente"], dict):
                    inner = dict(data["contribuyente"])
                    inner.update({k: v for k, v in data.items() if k != "contribuyente"})
                    return inner
                return data
            if (
                payload.get("razonSocial")
                or payload.get("nombre")
                or payload.get("rut")
            ):
                return payload
        raise SIIServiceError("Respuesta de contribuyente sin datos")

    def _merge_contribuyente(self, base: dict[str, str], patch: dict[str, str]) -> dict[str, str]:
        out = dict(base)
        for key, val in patch.items():
            if val and not out.get(key):
                out[key] = val
        return out

    def _normalize_datos_receptor(self, data: dict[str, Any]) -> dict[str, str]:
        giro = self._pick_str(data, "giro", "Giro")
        if not giro:
            giros = data.get("girosDisponibles")
            if isinstance(giros, list) and giros:
                first = giros[0]
                if isinstance(first, dict):
                    giro = self._pick_str(first, "descripcion", "descripcionGiro")
        direccion = self._pick_str(data, "direccion", "Direccion")
        comuna = self._pick_str(data, "comuna", "Comuna")
        ciudad = self._pick_str(data, "ciudad", "Ciudad")
        if not direccion:
            dirs = data.get("direccionesDisponibles")
            if isinstance(dirs, list) and dirs:
                first = dirs[0]
                if isinstance(first, dict):
                    direccion = self._pick_str(first, "direccion")
                    comuna = comuna or self._pick_str(first, "comuna")
                    ciudad = ciudad or self._pick_str(first, "ciudad")
        razon = self._pick_str(data, "razonSocial", "razon_social", "nombre")
        if self._is_masked_sii_text(razon):
            razon = ""
        if self._is_masked_sii_text(giro):
            giro = ""
        return {
            "razon_social": razon,
            "giro": giro,
            "direccion": direccion,
            "comuna": comuna,
            "ciudad": ciudad,
            "region": "",
            "estado_sii": "REGISTRO SII",
        }

    def _normalize_situacion_tributaria(self, data: dict[str, Any]) -> dict[str, str]:
        giro = self._pick_str(data, "glosaActividad", "giro", "Giro")
        acts = (
            data.get("actividadesEconomicas")
            or data.get("actividades_economicas")
            or data.get("actEcos")
        )
        if isinstance(acts, list) and acts:
            first = acts[0]
            if isinstance(first, dict):
                giro = giro or self._pick_str(
                    first, "descripcion", "descripcionActividad", "glosa"
                )
        nombre = self._pick_str(
            data,
            "nombre",
            "razonSocial",
            "razon_social",
            "Nombre",
            "name",
        )
        if not nombre:
            nombres = self._pick_str(data, "nombres")
            ap_pat = self._pick_str(data, "apellidoPaterno", "apellido_paterno")
            ap_mat = self._pick_str(data, "apellidoMaterno", "apellido_materno")
            nombre = " ".join(p for p in (nombres, ap_pat, ap_mat) if p).strip()
        estado = self._estado_from_situacion(data)
        if self._is_masked_sii_text(nombre):
            nombre = ""
        if self._is_masked_sii_text(giro):
            giro = ""
        return {
            "razon_social": nombre,
            "giro": giro,
            "direccion": "",
            "comuna": "",
            "ciudad": "",
            "region": "",
            "estado_sii": estado,
        }

    @staticmethod
    def _estado_from_situacion(data: dict[str, Any]) -> str:
        if data.get("terminoGiro"):
            return "TÉRMINO DE GIRO"
        if data.get("inicioActividades") is False:
            return "SIN INICIO DE ACTIVIDADES"
        cumple = (data.get("cumpleObligacionTributaria") or "").strip()
        if cumple:
            return f"VIGENTE ({cumple})"
        return "VIGENTE EN SII"

    def _resolve_region(self, comuna: str, ciudad: str, sii_region: str) -> str:
        geo = self._load_chile_geo()
        if comuna:
            matched = self._region_for_comuna(comuna, geo)
            if matched:
                return matched
        if sii_region:
            matched = self._match_geo_region_name(sii_region, geo)
            if matched:
                return matched
        if ciudad and ciudad.strip().lower() in {"santiago", "providencia", "las condes"}:
            return "Metropolitana de Santiago"
        return (sii_region or "").strip()

    @staticmethod
    def _norm_geo(value: str) -> str:
        import unicodedata

        s = unicodedata.normalize("NFKD", (value or "").strip().lower())
        return "".join(ch for ch in s if not unicodedata.combining(ch))

    def _region_for_comuna(self, comuna: str, geo: list[dict]) -> str | None:
        target = self._norm_geo(comuna)
        if not target:
            return None
        for region in geo:
            nombre = region.get("nombre") or ""
            comunas = region.get("comunas") or []
            for c in comunas:
                cn = self._norm_geo(str(c))
                if cn == target or target in cn or cn in target:
                    return nombre
        return None

    def _match_geo_region_name(self, sii_region: str, geo: list[dict]) -> str | None:
        target = self._norm_geo(sii_region).replace("region ", "")
        if not target:
            return None
        for region in geo:
            nombre = region.get("nombre") or ""
            rn = self._norm_geo(nombre)
            if rn == target or target in rn or rn in target:
                return nombre
            if "metropolitana" in target and "metropolitana" in rn:
                return nombre
        return None

    @staticmethod
    def _load_chile_geo() -> list[dict]:
        path = Path(__file__).resolve().parents[1] / "ventas" / "data" / "chile_geo.json"
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def consultar_rcv(self, periodo: str) -> list[dict[str, Any]]:
        if not self.configured():
            faltan = ", ".join(self.missing_config_labels())
            raise SIIServiceError(
                f"SII no configurado: revise .env y reinicie el servidor. Faltan: {faltan}"
            )
        periodo = self._validar_periodo(periodo)
        raw = self._fetch_rcv(periodo)
        return self._normalizar_documentos(raw, periodo)

    @staticmethod
    def _validar_periodo(periodo: str) -> str:
        p = (periodo or "").strip()
        if len(p) != 7 or p[4] != "-":
            raise SIIServiceError("Periodo inválido. Use formato YYYY-MM.")
        try:
            datetime.strptime(p + "-01", "%Y-%m-%d")
        except ValueError as exc:
            raise SIIServiceError("Periodo inválido. Use formato YYYY-MM.") from exc
        return p

    def _fetch_rcv(self, periodo: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.provider == "apigateway":
                    return self._fetch_apigateway(periodo)
                return self._fetch_baseapi(periodo)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "SII RCV intento %s/%s falló (%s): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    self.provider,
                    exc,
                )
                if attempt < self.max_retries:
                    time.sleep(0.8 * (attempt + 1))
        raise SIIServiceError(f"Error de conexión con API SII: {last_error}") from last_error

    def _fetch_baseapi(self, periodo: str) -> Any:
        """
        BaseAPI RCV ventas (documentos emitidos).
        Docs: POST /api/v1/sii/rcv/{periodo}/venta
        Header: x-api-key
        Body: rut, password (+ rut_empresa opcional)
        """
        url = f"{self.BASEAPI_URL}/api/v1/sii/rcv/{periodo}/venta"
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body: dict[str, str] = {
            "rut": self.rut_auth,
            "password": self.sii_password,
        }
        if self.rut_empresa:
            body["rut_empresa"] = self.rut_empresa

        resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
        return self._parse_http_response(resp)

    def _fetch_apigateway(self, periodo: str) -> Any:
        url = f"https://apigateway.cl/v2/sii/rcv/ventas/{periodo}"
        headers = {"x-api-key": self.api_key}
        params = {"rut": self.rut_empresa}
        if self.ambiente:
            params["ambiente"] = self.ambiente
        resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        return self._parse_http_response(resp)

    @staticmethod
    def _parse_http_response(resp: requests.Response) -> Any:
        if resp.status_code >= 400:
            body = (resp.text or "")[:500]
            raise SIIServiceError(f"API SII respondió HTTP {resp.status_code}: {body}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SIIServiceError("La API SII no devolvió JSON válido.") from exc

    def _normalizar_documentos(self, payload: Any, periodo: str) -> list[dict[str, Any]]:
        items = self._extraer_lista(payload)
        out: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            doc = self._normalizar_item(raw, periodo)
            if doc:
                out.append(doc)
        return out

    @staticmethod
    def _extraer_lista(payload: Any) -> list:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        if payload.get("success") is False:
            msg = payload.get("message") or payload.get("error") or "Error en API BaseAPI"
            raise SIIServiceError(str(msg))

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("datos", "documentos", "items", "registros"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
            inner = data.get("data")
            if isinstance(inner, list):
                return inner

        for key in (
            "documentos",
            "data",
            "items",
            "ventas",
            "registros",
            "dtes",
            "resultado",
            "results",
            "datos",
        ):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = val.get("datos") or val.get("documentos") or val.get("items") or val.get("data")
                if isinstance(nested, list):
                    return nested
        return []

    def _normalizar_item(self, raw: dict, periodo: str) -> dict[str, Any] | None:
        tipo = self._pick_str(
            raw, "tipo_dte", "tipoDte", "tipo", "codigoSii", "dte", "Tipo Doc", "tipoDoc"
        )
        tipo = "".join(ch for ch in tipo if ch.isdigit()) or tipo
        folio = self._pick_int(raw, "folio", "folioDoc", "numero", "nro", "Folio")
        if not tipo or folio is None:
            return None
        if tipo not in DTE_TIPOS_VALIDOS:
            tipo = tipo[:10]

        fecha = self._pick_date(
            raw, "fecha_emision", "fecha", "fechaEmision", "fchEmis", "Fecha Docto", "fechaDocto"
        )
        periodo_doc = periodo
        if fecha:
            periodo_doc = fecha.strftime("%Y-%m")

        estado = self._pick_estado(raw)
        neto = self._pick_int(
            raw, "monto_neto", "montoNeto", "neto", "mntNeto", "Monto Neto", "montoNeto"
        ) or 0
        iva = self._pick_int(
            raw, "monto_iva", "montoIva", "iva", "mntIva", "Monto IVA", "Monto IVA Recuperable"
        ) or 0
        total = self._pick_int(
            raw, "monto_total", "montoTotal", "total", "mntTotal", "Monto total", "Monto Total"
        )
        if total is None:
            total = neto + iva

        track = self._pick_str(raw, "track_id", "trackId", "trackID", "trackid") or None
        xml_ok = self._pick_bool(raw, "xml_disponible", "xmlDisponible", "tieneXml", "xml")

        return {
            "tipo_dte": tipo,
            "folio": int(folio),
            "fecha_emision": fecha,
            "rut_receptor": self._pick_str(
                raw,
                "rut_receptor",
                "rutReceptor",
                "rut",
                "rutCliente",
                "RUT Cliente",
                "RUT Receptor",
                "RUT Proveedor",
            )
            or None,
            "razon_social_receptor": self._pick_str(
                raw,
                "razon_social_receptor",
                "razonSocial",
                "razonSocialReceptor",
                "nombre",
                "cliente",
                "Razon Social",
            )
            or None,
            "monto_neto": int(neto),
            "monto_iva": int(iva),
            "monto_total": int(total),
            "estado_sii": estado,
            "track_id": track,
            "xml_disponible": bool(xml_ok),
            "periodo": periodo_doc,
            "notas": self._pick_str(raw, "notas", "observacion", "glosa") or None,
        }

    @staticmethod
    def _pick(raw: dict, *keys: str) -> Any:
        for k in keys:
            if k in raw and raw[k] is not None:
                return raw[k]
        return None

    def _pick_str(self, raw: dict, *keys: str) -> str:
        val = self._pick(raw, *keys)
        return str(val).strip() if val is not None else ""

    def _pick_int(self, raw: dict, *keys: str) -> int | None:
        val = self._pick(raw, *keys)
        if val is None or val == "":
            return None
        try:
            if isinstance(val, float):
                return int(round(val))
            s = str(val).strip().replace(".", "").replace(",", ".")
            if "." in s:
                return int(round(float(s)))
            return int(s)
        except (TypeError, ValueError):
            return None

    def _pick_bool(self, raw: dict, *keys: str) -> bool:
        val = self._pick(raw, *keys)
        if isinstance(val, bool):
            return val
        if val is None:
            return False
        return str(val).strip().lower() in {"1", "true", "yes", "si", "sí"}

    def _pick_date(self, raw: dict, *keys: str) -> date | None:
        val = self._pick(raw, *keys)
        if val is None:
            return None
        if isinstance(val, date) and not isinstance(val, datetime):
            return val
        if isinstance(val, datetime):
            return val.date()
        s = str(val).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def _pick_estado(self, raw: dict) -> str:
        raw_est = self._pick_str(raw, "estado_sii", "estadoSii", "estado", "status").upper()
        if raw_est in ESTADOS_VALIDOS:
            return raw_est
        if any(x in raw_est for x in ("RECH", "ANUL", "ERROR")):
            return "RECHAZADO"
        if any(x in raw_est for x in ("ACEPT", "OK", "VIG")):
            return "ACEPTADO"
        return "PENDIENTE"
