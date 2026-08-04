from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app.extensions import db
from app.utils.decorators import login_required
from app.utils.permissions import has_permission
from .models import VehiculoVin

vehiculos_vin_bp = Blueprint(
    "vehiculos_vin",
    __name__,
    url_prefix="/vehiculos-vin",
    template_folder="../../templates",
)

_VIN_CLEAN_RE = re.compile(r"[^A-Za-z0-9]")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# 1.5 / 2,0 / 1.0T / 1.5TURBO / 1.6L (nunca enteros sueltos: ARRIZO 3, TIGGO 7)
_CILINDRADA_RE = re.compile(
    r"^(\d+[.,]\d+)\s*(T|TURBO|L|TFSI|TSI|GDI|GTE)?$",
    re.IGNORECASE,
)
_TRANSMISION_TOKENS = {
    "CVT", "AT", "MT", "DCT", "DSG", "AUT", "AUTO",
    "AUTOMATICA", "AUTOMÁTICA", "MANUAL",
}
_IMG_ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_IMG_MAX_BYTES = 4 * 1024 * 1024
_IMG_THUMB = (1280, 960)  # suficiente para ver en grande
_IMG_GALLERY_MAX = 8


@vehiculos_vin_bp.before_request
def _vehiculos_vin_module_guard():
    if "user" not in session:
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_vehiculos_vin"):
        return None
    flash("No tienes permisos para acceder al registro VIN / Chasis.", "error")
    return redirect(url_for("productos.buscar"))


def _current_user() -> str:
    return (session.get("user") or "sistema").strip() or "sistema"


def _imagenes_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "data" / "vehiculos_vin_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slot_path(vid: int, slot: int) -> Path:
    """slot 1 = imagen principal histórica ({vid}.jpg); 2..N = {vid}_gN.jpg."""
    s = max(1, int(slot))
    if s <= 1:
        return _imagenes_root() / f"{int(vid)}.jpg"
    return _imagenes_root() / f"{int(vid)}_g{s}.jpg"


def _imagen_local_path(vid: int) -> Path:
    return _slot_path(vid, 1)


def _borrar_imagen_local(vid: int) -> None:
    """Compat: borra solo la imagen principal local."""
    p = _imagen_local_path(vid)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def _borrar_galeria_local(vid: int) -> None:
    for slot in range(1, _IMG_GALLERY_MAX + 1):
        p = _slot_path(vid, slot)
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def _imagen_display_url(v: VehiculoVin) -> str:
    """URL de la imagen principal (compat con pantallas existentes)."""
    url = (v.imagen_url or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if _imagen_local_path(v.id).is_file():
        return url_for("vehiculos_vin.archivo_imagen", vid=v.id)
    return ""


def _list_imagenes(v: VehiculoVin) -> list[dict[str, Any]]:
    """Galería ordenada: principal + extras locales. No rompe registros viejos."""
    out: list[dict[str, Any]] = []
    primary = _imagen_display_url(v)
    if primary:
        out.append(
            {
                "slot": 1,
                "url": primary,
                "url_quitar": url_for("vehiculos_vin.quitar_imagen", vid=v.id) + "?slot=1",
            }
        )
    for slot in range(2, _IMG_GALLERY_MAX + 1):
        if _slot_path(v.id, slot).is_file():
            out.append(
                {
                    "slot": slot,
                    "url": url_for("vehiculos_vin.archivo_imagen_slot", vid=v.id, slot=slot),
                    "url_quitar": url_for("vehiculos_vin.quitar_imagen", vid=v.id)
                    + f"?slot={slot}",
                }
            )
    return out


def _siguiente_slot_libre(v: VehiculoVin) -> int | None:
    if not _imagen_display_url(v):
        return 1
    for slot in range(2, _IMG_GALLERY_MAX + 1):
        if not _slot_path(v.id, slot).is_file():
            return slot
    return None


def _guardar_imagen_en_path(dest: Path, file_storage) -> tuple[bool, str]:
    if file_storage is None or not getattr(file_storage, "filename", None):
        return False, "No se recibió archivo"
    filename = secure_filename(file_storage.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in _IMG_ALLOWED:
        return False, "Formato no permitido. Usa JPG, PNG o WEBP."
    raw = file_storage.read()
    if not raw:
        return False, "Archivo vacío"
    if len(raw) > _IMG_MAX_BYTES:
        return False, "La imagen no puede superar 4 MB"
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail(_IMG_THUMB, Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        return False, f"No se pudo procesar la imagen: {exc}"
    return True, ""


def _guardar_imagen_local(vid: int, file_storage) -> tuple[bool, str]:
    """Compat: guarda/reemplaza la imagen principal."""
    return _guardar_imagen_en_path(_imagen_local_path(vid), file_storage)


def normalizar_vin(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            if float(raw) == int(raw):
                raw = str(int(raw))
            else:
                raw = str(raw)
        except (ValueError, OverflowError):
            raw = str(raw)
    s = _VIN_CLEAN_RE.sub("", str(raw).strip().upper())
    return s or None


def normalizar_texto(raw: Any, max_len: int | None = None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, float) and pd.isna(raw):
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            if float(raw) == int(raw):
                s = str(int(raw))
            else:
                s = str(raw).strip()
        except (ValueError, OverflowError):
            s = str(raw).strip()
    else:
        s = str(raw).strip()
    if not s or s.lower() == "nan":
        return ""
    if max_len is not None:
        return s[:max_len]
    return s


def normalizar_anio(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if 1950 <= n <= 2100:
        return n
    return None


def desglosar_modelo(texto: str, marca: str | None = None) -> dict[str, Any]:
    """
    Separa textos tipo 'ARRIZO 5 GLS CVT 1.5 2017' o 'AX5 2019 1.6'
    en modelo / año / cilindrada / transmisión.
    El año puede ir al medio (antes de la cilindrada), no solo al final.
    """
    completo = normalizar_texto(texto, 200).upper()
    result: dict[str, Any] = {
        "modelo_completo": completo,
        "modelo": completo,
        "anio": None,
        "cilindrada": "",
        "transmision": "",
    }
    if not completo:
        return result

    # Quitar notas entre paréntesis para el parseo (se conservan en modelo_completo)
    parse_src = re.sub(r"\([^)]*\)", " ", completo)
    tokens = [t for t in re.split(r"\s+", parse_src) if t]
    anio = None
    cilindrada = ""
    transmision_bits: list[str] = []
    kept: list[str] = []

    for tok in tokens:
        # Año en cualquier posición (ej. AX5 2019 1.6)
        if _YEAR_RE.match(tok):
            anio = int(tok)
            continue
        if tok in _TRANSMISION_TOKENS or tok in {"4X4", "4X2", "AWD", "2WD"}:
            transmision_bits.append(tok)
            continue
        # Cilindrada: 1.5 / 2,0 / 1.0T / 1.5TURBO / 1.6L
        cand = tok.replace(",", ".")
        m_cil = _CILINDRADA_RE.match(cand)
        if m_cil:
            num = m_cil.group(1)
            suf = (m_cil.group(2) or "").upper()
            try:
                val = float(num)
            except ValueError:
                kept.append(tok)
                continue
            if 0.5 <= val <= 8.0:
                if suf in {"T", "TURBO", "TFSI", "TSI", "GTE"}:
                    cilindrada = f"{num}T"
                elif suf == "L":
                    cilindrada = f"{num}L"
                else:
                    cilindrada = num
                continue
        # Palabras de ruido habituales en Excel
        if tok in {"BENCINERA", "DIESEL", "DIESEL", "GASOLINA", "PERU", "OPTICO", "FONDO", "CROMADO"}:
            continue
        kept.append(tok)

    # Quitar marca al inicio si viene repetida en el modelo
    marca_u = normalizar_texto(marca, 80).upper()
    if marca_u and kept:
        marca_parts = marca_u.split()
        if len(kept) >= len(marca_parts) and kept[: len(marca_parts)] == marca_parts:
            kept = kept[len(marca_parts) :]

    modelo = " ".join(kept).strip()
    result["modelo"] = modelo or completo
    result["anio"] = anio
    result["cilindrada"] = cilindrada
    result["transmision"] = " ".join(transmision_bits)
    return result


def _q_tokens(q: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", (q or "").strip()) if t]


def _apply_search(query, q: str):
    tokens = _q_tokens(q)
    if not tokens:
        return query
    for token in tokens:
        like = f"%{token}%"
        vin_norm = normalizar_vin(token) or token.upper()
        query = query.filter(
            or_(
                VehiculoVin.vin.ilike(f"%{vin_norm}%"),
                VehiculoVin.chasis.ilike(like),
                VehiculoVin.marca.ilike(like),
                VehiculoVin.modelo.ilike(like),
                VehiculoVin.modelo_completo.ilike(like),
                VehiculoVin.motor.ilike(like),
                VehiculoVin.version.ilike(like),
                VehiculoVin.patente.ilike(like),
                VehiculoVin.nombre_china.ilike(like),
                VehiculoVin.transmision.ilike(like),
                VehiculoVin.cilindrada.ilike(like),
                VehiculoVin.notas.ilike(like),
            )
        )
    return query


def _sync_vin_chasis(vin: str | None, chasis: str | None) -> tuple[str | None, str | None]:
    """Para Andes, VIN y chasis son lo mismo: se espejan si falta uno."""
    if vin and not chasis:
        return vin, vin
    if chasis and not vin:
        chasis_n = normalizar_vin(chasis) or chasis.upper()
        return chasis_n, chasis_n
    if vin and chasis and vin != normalizar_vin(chasis):
        # Prioriza VIN; deja chasis = VIN para consistencia operativa
        return vin, vin
    return vin, chasis


def _build_vehicle_data(
    *,
    vin: str | None,
    chasis: str | None,
    marca: str,
    modelo_raw: str,
    anio_explicit: int | None,
    motor: str,
    version: str,
    transmision_explicit: str,
    cilindrada_explicit: str,
    patente: str,
    nombre_china: str,
    notas: str,
    auto_desglosar: bool = True,
) -> dict[str, Any]:
    vin, chasis = _sync_vin_chasis(vin, chasis)
    parsed = desglosar_modelo(modelo_raw, marca) if auto_desglosar else {
        "modelo_completo": normalizar_texto(modelo_raw, 200).upper(),
        "modelo": normalizar_texto(modelo_raw, 160).upper(),
        "anio": None,
        "cilindrada": "",
        "transmision": "",
    }

    modelo = parsed["modelo"]
    modelo_completo = parsed["modelo_completo"] or modelo
    anio = anio_explicit if anio_explicit is not None else parsed["anio"]
    cilindrada = cilindrada_explicit or parsed["cilindrada"]
    transmision = transmision_explicit or parsed["transmision"]

    return {
        "vin": vin,
        "chasis": chasis,
        "marca": marca,
        "modelo": modelo[:160] if modelo else "",
        "modelo_completo": modelo_completo[:200] if modelo_completo else "",
        "anio": anio,
        "motor": motor,
        "version": version,
        "transmision": transmision[:80] if transmision else "",
        "cilindrada": cilindrada[:40] if cilindrada else "",
        "patente": patente,
        "nombre_china": nombre_china,
        "notas": notas,
    }


def _form_payload() -> dict[str, Any]:
    """Alta manual: desglosa modelo compuesto; año/cilindrada del form pisan si vienen llenos."""
    return _build_vehicle_data(
        vin=normalizar_vin(request.form.get("vin")),
        chasis=normalizar_texto(request.form.get("chasis"), 64) or None,
        marca=normalizar_texto(request.form.get("marca"), 80).upper(),
        modelo_raw=normalizar_texto(request.form.get("modelo"), 200),
        anio_explicit=normalizar_anio(request.form.get("anio")),
        motor=normalizar_texto(request.form.get("motor"), 120).upper(),
        version=normalizar_texto(request.form.get("version"), 120).upper(),
        transmision_explicit=normalizar_texto(request.form.get("transmision"), 80).upper(),
        cilindrada_explicit=normalizar_texto(request.form.get("cilindrada"), 40).upper(),
        patente=normalizar_texto(request.form.get("patente"), 20).upper(),
        nombre_china=normalizar_texto(request.form.get("nombre_china"), 120).upper(),
        notas=normalizar_texto(request.form.get("notas")),
        auto_desglosar=True,
    )


def _validate_identity(vin: str | None, chasis: str | None) -> str | None:
    if not vin and not chasis:
        return "Debes indicar el VIN / chasis."
    return None


def _find_duplicate(vin: str | None, chasis: str | None, exclude_id: int | None = None) -> VehiculoVin | None:
    if vin:
        q = VehiculoVin.query.filter(VehiculoVin.vin == vin)
        if exclude_id is not None:
            q = q.filter(VehiculoVin.id != exclude_id)
        found = q.first()
        if found:
            return found
    if chasis:
        q = VehiculoVin.query.filter(VehiculoVin.chasis == chasis)
        if exclude_id is not None:
            q = q.filter(VehiculoVin.id != exclude_id)
        if vin:
            q = q.filter(or_(VehiculoVin.vin.is_(None), VehiculoVin.vin == "", VehiculoVin.vin == vin))
        return q.first()
    return None


def _modelo_busqueda_catalogo(v: VehiculoVin) -> str:
    """Preferir texto completo original; si no, modelo limpio."""
    completo = (v.modelo_completo or "").strip()
    if completo:
        return completo
    modelo = (v.modelo or "").strip()
    if modelo:
        return modelo
    partes = [p for p in ((v.marca or "").strip(), (v.cilindrada or "").strip()) if p]
    return " ".join(partes)


def _apply_data(target: VehiculoVin, data: dict[str, Any], *, overwrite_empty: bool = True) -> None:
    for key, value in data.items():
        if not overwrite_empty and (value is None or value == ""):
            continue
        setattr(target, key, value)


@vehiculos_vin_bp.route("/")
@login_required
def index():
    q = (request.args.get("q") or "").strip()
    solo_activos = (request.args.get("estado") or "activos") != "todos"
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = 50

    query = VehiculoVin.query
    if solo_activos:
        query = query.filter(VehiculoVin.activo.is_(True))
    query = _apply_search(query, q)
    query = query.order_by(VehiculoVin.marca.asc(), VehiculoVin.modelo.asc(), VehiculoVin.id.desc())

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    total_activos = VehiculoVin.query.filter(VehiculoVin.activo.is_(True)).count()
    total_registros = VehiculoVin.query.count()

    return render_template(
        "vehiculos_vin/index.html",
        items=items,
        q=q,
        page=page,
        total_pages=total_pages,
        total=total,
        total_activos=total_activos,
        total_registros=total_registros,
        solo_activos=solo_activos,
        active_page="vehiculos_vin",
    )


def _wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


@vehiculos_vin_bp.route("/nuevo", methods=["POST"])
@login_required
def nuevo():
    wants_json = _wants_json()
    data = _form_payload()
    err = _validate_identity(data["vin"], data["chasis"])
    if err:
        if wants_json:
            return jsonify({"ok": False, "error": err}), 400
        flash(err, "error")
        return redirect(url_for("vehiculos_vin.index"))

    dup = _find_duplicate(data["vin"], data["chasis"])
    if dup:
        msg = f"Ya existe un vehículo con ese VIN/chasis (#{dup.id})."
        if wants_json:
            return jsonify({"ok": False, "error": msg, "id": dup.id}), 409
        flash(msg, "error")
        return redirect(url_for("vehiculos_vin.index"))

    user = _current_user()
    v = VehiculoVin(
        **data,
        fuente="manual",
        activo=True,
        usuario_alta=user,
        usuario_edicion=user,
    )
    db.session.add(v)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        msg = "No se pudo guardar el vehículo (VIN duplicado)."
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 409
        flash(msg, "error")
        return redirect(url_for("vehiculos_vin.index"))
    except Exception:
        db.session.rollback()
        msg = "No se pudo guardar el vehículo."
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 500
        flash(msg, "error")
        return redirect(url_for("vehiculos_vin.index"))

    msg = "Vehículo registrado correctamente."
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "message": msg,
                "id": v.id,
                "vin": v.vin or v.chasis or "",
                "etiqueta": v.etiqueta or "",
            }
        )
    flash(msg, "success")
    return redirect(url_for("vehiculos_vin.index"))


@vehiculos_vin_bp.route("/<int:vid>")
@login_required
def detalle(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        flash("Vehículo no encontrado.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    q_catalogo = _modelo_busqueda_catalogo(v)
    return render_template(
        "vehiculos_vin/detalle.html",
        v=v,
        q_catalogo=q_catalogo,
        imagen_url=_imagen_display_url(v),
        imagenes=_list_imagenes(v),
        imagenes_max=_IMG_GALLERY_MAX,
        active_page="vehiculos_vin",
    )


@vehiculos_vin_bp.route("/<int:vid>/json")
@login_required
def ficha_json(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        return jsonify({"ok": False, "error": "Vehículo no encontrado"}), 404
    q_catalogo = _modelo_busqueda_catalogo(v)
    img = _imagen_display_url(v)
    imagenes = _list_imagenes(v)
    return jsonify(
        {
            "ok": True,
            "id": v.id,
            "vin": v.vin or "",
            "chasis": v.chasis or "",
            "marca": v.marca or "",
            "modelo": v.modelo or "",
            "modelo_completo": v.modelo_completo or "",
            "anio": v.anio,
            "cilindrada": v.cilindrada or "",
            "motor": v.motor or "",
            "transmision": v.transmision or "",
            "patente": v.patente or "",
            "nombre_china": v.nombre_china or "",
            "notas": v.notas or "",
            "imagen_url": img,
            "imagenes": imagenes,
            "imagenes_max": _IMG_GALLERY_MAX,
            "activo": bool(v.activo),
            "fuente": v.fuente or "",
            "etiqueta": v.etiqueta(),
            "q_catalogo": q_catalogo,
            "url_editar": url_for("vehiculos_vin.editar", vid=v.id),
            "url_catalogo": url_for("vehiculos_vin.productos_compatibles", vid=v.id) if q_catalogo else "",
            "url_toggle": url_for("vehiculos_vin.toggle_activo", vid=v.id),
            "url_eliminar": url_for("vehiculos_vin.eliminar", vid=v.id),
            "url_imagen": url_for("vehiculos_vin.subir_imagen", vid=v.id),
            "url_quitar_imagen": url_for("vehiculos_vin.quitar_imagen", vid=v.id),
        }
    )


def _safe_next_redirect(default_endpoint: str, **kwargs):
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/vehiculos-vin"):
        return redirect(nxt)
    return redirect(url_for(default_endpoint, **kwargs))


@vehiculos_vin_bp.route("/<int:vid>/editar", methods=["POST"])
@login_required
def editar(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        flash("Vehículo no encontrado.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    # En edición manual respetamos los campos enviados; desglose solo si modelo cambió y año vacío
    modelo_raw = normalizar_texto(request.form.get("modelo"), 200)
    marca = normalizar_texto(request.form.get("marca"), 80).upper()
    anio_form = normalizar_anio(request.form.get("anio"))
    cil_form = normalizar_texto(request.form.get("cilindrada"), 40).upper()
    tr_form = normalizar_texto(request.form.get("transmision"), 80).upper()

    # Si el usuario pegó un modelo compuesto y no llenó año/cilindrada, desglosar
    auto = not anio_form and not cil_form
    data = _build_vehicle_data(
        vin=normalizar_vin(request.form.get("vin")),
        chasis=normalizar_texto(request.form.get("chasis"), 64) or None,
        marca=marca,
        modelo_raw=modelo_raw,
        anio_explicit=anio_form,
        motor=normalizar_texto(request.form.get("motor"), 120).upper(),
        version=normalizar_texto(request.form.get("version"), 120).upper(),
        transmision_explicit=tr_form,
        cilindrada_explicit=cil_form,
        patente=normalizar_texto(request.form.get("patente"), 20).upper(),
        nombre_china=normalizar_texto(request.form.get("nombre_china"), 120).upper(),
        notas=normalizar_texto(request.form.get("notas")),
        auto_desglosar=auto,
    )

    err = _validate_identity(data["vin"], data["chasis"])
    if err:
        flash(err, "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)

    dup = _find_duplicate(data["vin"], data["chasis"], exclude_id=vid)
    if dup:
        flash(f"Otro registro ya usa ese VIN/chasis (#{dup.id}).", "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)

    _apply_data(v, data, overwrite_empty=True)
    v.usuario_edicion = _current_user()
    v.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("No se pudo actualizar (VIN duplicado).", "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)
    except Exception:
        db.session.rollback()
        flash("No se pudo actualizar el vehículo.", "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)

    flash("Vehículo actualizado.", "success")
    return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)


@vehiculos_vin_bp.route("/<int:vid>/toggle-activo", methods=["POST"])
@login_required
def toggle_activo(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        flash("Vehículo no encontrado.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    v.activo = not bool(v.activo)
    v.usuario_edicion = _current_user()
    v.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Vehículo reactivado." if v.activo else "Vehículo desactivado.", "success")
    return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)


@vehiculos_vin_bp.route("/<int:vid>/eliminar", methods=["POST"])
@login_required
def eliminar(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        flash("Vehículo no encontrado.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    _borrar_galeria_local(vid)
    db.session.delete(v)
    db.session.commit()
    flash("Vehículo eliminado del registro VIN.", "success")
    return _safe_next_redirect("vehiculos_vin.index")


@vehiculos_vin_bp.route("/<int:vid>/imagen", methods=["GET"])
@login_required
def archivo_imagen(vid: int):
    return archivo_imagen_slot(vid, 1)


@vehiculos_vin_bp.route("/<int:vid>/imagen/<int:slot>", methods=["GET"])
@login_required
def archivo_imagen_slot(vid: int, slot: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        return ("", 404)
    slot = int(slot or 1)
    if slot < 1 or slot > _IMG_GALLERY_MAX:
        return ("", 404)
    # Slot 1 puede ser solo Cloudinary (sin archivo local)
    if slot == 1:
        cloud = (v.imagen_url or "").strip()
        if cloud.startswith("http://") or cloud.startswith("https://"):
            return redirect(cloud)
    path = _slot_path(vid, slot)
    if not path.is_file():
        return ("", 404)
    return send_file(path, mimetype="image/jpeg", max_age=86400)


@vehiculos_vin_bp.route("/<int:vid>/imagen", methods=["POST"])
@login_required
def subir_imagen(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        return jsonify({"ok": False, "error": "Vehículo no encontrado"}), 404

    wants_json = _wants_json()
    append_flag = (
        request.form.get("append")
        or request.args.get("append")
        or ("1" if wants_json else "")
    )
    append = str(append_flag).strip().lower() in {"1", "true", "yes", "on"}

    f = request.files.get("imagen") or request.files.get("file")
    if append:
        slot = _siguiente_slot_libre(v)
        if slot is None:
            msg = f"Máximo {_IMG_GALLERY_MAX} imágenes por vehículo."
            if wants_json:
                return jsonify({"ok": False, "error": msg}), 400
            flash(msg, "error")
            return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)
    else:
        # Formulario clásico de detalle: reemplaza solo la principal
        slot = 1

    dest = _slot_path(vid, slot)
    ok, err = _guardar_imagen_en_path(dest, f)
    if not ok:
        if wants_json:
            return jsonify({"ok": False, "error": err}), 400
        flash(err, "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)

    # Cloudinary solo para la principal (compatibilidad)
    if slot == 1:
        cloud_url = ""
        try:
            from app.utils.cloudinary_config import is_configured, upload_image

            if is_configured() and dest.is_file():
                result = upload_image(
                    dest,
                    folder="andes_erp/vehiculos_vin",
                    public_id=f"vin_{vid}",
                )
                cloud_url = (result.get("url") or "").strip()
        except Exception:
            cloud_url = ""
        v.imagen_url = cloud_url

    v.usuario_edicion = _current_user()
    v.updated_at = datetime.utcnow()
    db.session.commit()

    imagenes = _list_imagenes(v)
    display = _imagen_display_url(v)
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "imagen_url": display,
                "imagenes": imagenes,
                "slot": slot,
            }
        )
    flash("Imagen del vehículo actualizada." if slot == 1 else "Imagen agregada a la galería.", "success")
    return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)


@vehiculos_vin_bp.route("/<int:vid>/quitar-imagen", methods=["POST"])
@login_required
def quitar_imagen(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        return jsonify({"ok": False, "error": "Vehículo no encontrado"}), 404

    wants_json = _wants_json()
    try:
        slot = int(request.args.get("slot") or request.form.get("slot") or 1)
    except (TypeError, ValueError):
        slot = 1
    if slot < 1 or slot > _IMG_GALLERY_MAX:
        msg = "Imagen inválida."
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "error")
        return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)

    if slot == 1:
        _borrar_imagen_local(vid)
        v.imagen_url = ""
    else:
        p = _slot_path(vid, slot)
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass

    v.usuario_edicion = _current_user()
    v.updated_at = datetime.utcnow()
    db.session.commit()

    imagenes = _list_imagenes(v)
    if wants_json:
        return jsonify(
            {
                "ok": True,
                "imagen_url": _imagen_display_url(v),
                "imagenes": imagenes,
            }
        )
    flash("Imagen eliminada.", "success")
    return _safe_next_redirect("vehiculos_vin.detalle", vid=vid)


@vehiculos_vin_bp.route("/<int:vid>/productos")
@login_required
def productos_compatibles(vid: int):
    v = db.session.get(VehiculoVin, vid)
    if not v:
        flash("Vehículo no encontrado.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    q = _modelo_busqueda_catalogo(v)
    if not q:
        flash(
            "Este vehículo no tiene modelo para buscar en el catálogo. Completa el modelo primero.",
            "error",
        )
        return redirect(url_for("vehiculos_vin.detalle", vid=vid))

    return redirect(url_for("productos.buscar", q=q))


def _reparse_vehicle(v: VehiculoVin) -> bool:
    """Desglosa modelo_completo o modelo. Devuelve True si cambió."""
    fuente = (v.modelo_completo or "").strip() or (v.modelo or "").strip()
    if not fuente:
        changed = False
        if v.vin and not v.chasis:
            v.chasis = v.vin
            changed = True
        elif v.chasis and not v.vin:
            v.vin = normalizar_vin(v.chasis) or v.chasis
            changed = True
        return changed

    parsed = desglosar_modelo(fuente, v.marca)
    changed = False

    if not (v.modelo_completo or "").strip() and parsed["modelo_completo"]:
        v.modelo_completo = parsed["modelo_completo"][:200]
        changed = True

    new_modelo = (parsed["modelo"] or "")[:160]
    if new_modelo and new_modelo != (v.modelo or "").strip().upper():
        # Solo pisar modelo si la fuente trae año/cilindrada (es compuesto) o modelo_completo existe
        if parsed["anio"] or parsed["cilindrada"] or (v.modelo_completo or "").strip():
            v.modelo = new_modelo
            changed = True

    if parsed["anio"] is not None and v.anio != parsed["anio"]:
        v.anio = parsed["anio"]
        changed = True
    if parsed["cilindrada"] and (v.cilindrada or "").strip() != parsed["cilindrada"]:
        v.cilindrada = parsed["cilindrada"][:40]
        changed = True
    if parsed["transmision"] and not (v.transmision or "").strip():
        v.transmision = parsed["transmision"][:80]
        changed = True
    if v.vin and not v.chasis:
        v.chasis = v.vin
        changed = True
    elif v.chasis and not v.vin:
        v.vin = normalizar_vin(v.chasis) or v.chasis
        changed = True

    return changed


@vehiculos_vin_bp.route("/desglosar-existentes", methods=["POST"])
@login_required
def desglosar_existentes():
    """Relee modelos ya cargados y separa año / cilindrada / modelo limpio."""
    n = 0
    for v in VehiculoVin.query.all():
        if _reparse_vehicle(v):
            v.usuario_edicion = _current_user()
            v.updated_at = datetime.utcnow()
            n += 1
    db.session.commit()
    flash(f"Desglose listo: {n} vehículo(s) actualizado(s).", "success")
    return redirect(url_for("vehiculos_vin.index"))


def _normalize_column_name(name: str) -> str:
    return "".join(ch.lower() for ch in str(name or "") if ch.isalnum())


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "vin": (
        "vin",
        "vinchasis",
        "nrovin",
        "numerovin",
        "chassisvin",
        "chassisno",
        "chasisvin",
        "chasis",
        "chassis",
        "nrochasis",
        "numerochasis",
    ),
    "chasis": ("chasisalt", "chasisalternativo"),
    "marca": ("marca", "brand", "make"),
    "modelo": ("modelo", "model", "modelovehiculo", "vehiculomodelo"),
    "anio": ("anio", "ano", "año", "year", "aniofabricacion"),
    "motor": (
        "motor",
        "nmotor",
        "engine",
        "codigomotor",
        "numeromotor",
        "nromotor",
        "numerodemotor",
        "engineno",
        "enginenumber",
    ),
    "version": ("version", "versión", "trim", "variante"),
    "transmision": ("transmision", "transmisión", "transmission", "caja"),
    "cilindrada": ("cilindrada", "cc", "displacement"),
    "patente": ("patente", "placa", "plate", "ppua"),
    "nombre_china": (
        "nombreenchina",
        "nombrechina",
        "chinename",
        "namechina",
        "codigochina",
        "china",
    ),
    "notas": ("notas", "nota", "observaciones", "obs", "comentario", "comments"),
}


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {_normalize_column_name(k): v for k, v in row.items()}
    out: dict[str, Any] = {}
    for field, aliases in _COLUMN_ALIASES.items():
        value = None
        for alias in aliases:
            if alias in normalized and normalized[alias] is not None:
                raw = normalized[alias]
                if isinstance(raw, float) and pd.isna(raw):
                    continue
                if str(raw).strip() == "" or str(raw).strip().lower() == "nan":
                    continue
                value = raw
                break
        out[field] = value
    return out


def _vehicles_to_dataframe(items: list[VehiculoVin]) -> pd.DataFrame:
    """Columnas limpias para trabajar: sin duplicar VIN/chasis ni modelo_completo."""
    rows = []
    for v in items:
        rows.append(
            {
                "VIN / Chasis": v.vin or v.chasis or "",
                "Marca": v.marca or "",
                "Modelo": v.modelo or "",
                "Año": v.anio if v.anio is not None else "",
                "Cilindrada": v.cilindrada or "",
                "N° Motor": v.motor or "",
                "Transmisión": v.transmision or "",
                "Patente": v.patente or "",
                "Nombre en China": v.nombre_china or "",
                "Notas": v.notas or "",
                "Activo": "SI" if v.activo else "NO",
                "Fuente": v.fuente or "",
            }
        )
    return pd.DataFrame(rows)


def _excel_response(df: pd.DataFrame, filename: str):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="vehiculos_vin")
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@vehiculos_vin_bp.route("/plantilla.xlsx")
@login_required
def plantilla_excel():
    df = pd.DataFrame(
        [
            {
                "VIN / Chasis": "LSJA24U62HS000001",
                "Marca": "CHERY",
                "Modelo": "ARRIZO 5 GLS CVT 1.5 2017",
                "Año": 2017,
                "Cilindrada": "1.5",
                "N° Motor": "SQR481FAF8F04541",
                "Transmisión": "CVT",
                "Patente": "ABCD12",
                "Nombre en China": "A21",
                "Notas": "Ejemplo — bórralo antes de importar",
            }
        ]
    )
    return _excel_response(df, "plantilla_vehiculos_vin.xlsx")


@vehiculos_vin_bp.route("/exportar.xlsx")
@login_required
def exportar_excel():
    """
    scope=visible → página actual (lo que se ve en pantalla)
    scope=all     → todo el registro VIN (completo)
    """
    scope = (request.args.get("scope") or "all").strip().lower()
    q = (request.args.get("q") or "").strip()
    solo_activos = (request.args.get("estado") or "activos") != "todos"
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = 50

    if scope == "visible":
        query = VehiculoVin.query
        if solo_activos:
            query = query.filter(VehiculoVin.activo.is_(True))
        query = _apply_search(query, q)
        query = query.order_by(VehiculoVin.marca.asc(), VehiculoVin.modelo.asc(), VehiculoVin.id.desc())
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        fname = "vehiculos_vin_pantalla.xlsx"
    else:
        items = (
            VehiculoVin.query.order_by(
                VehiculoVin.marca.asc(), VehiculoVin.modelo.asc(), VehiculoVin.id.desc()
            ).all()
        )
        fname = "vehiculos_vin_completo.xlsx"

    return _excel_response(_vehicles_to_dataframe(items), fname)


@vehiculos_vin_bp.route("/importar", methods=["POST"])
@login_required
def importar():
    archivo = request.files.get("archivo")
    if not archivo or not (archivo.filename or "").strip():
        flash("Selecciona un archivo Excel o CSV.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    filename = (archivo.filename or "").lower()
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(archivo)
        else:
            df = pd.read_excel(archivo)
    except Exception as exc:
        flash(f"No se pudo leer el archivo: {exc}", "error")
        return redirect(url_for("vehiculos_vin.index"))

    if df is None or df.empty:
        flash("El archivo no tiene filas.", "error")
        return redirect(url_for("vehiculos_vin.index"))

    user = _current_user()
    creados = 0
    actualizados = 0
    omitidos = 0
    errores: list[str] = []

    # Caché en memoria: evita UNIQUE al reimportar y duplicados dentro del mismo Excel
    by_vin: dict[str, VehiculoVin] = {
        v.vin: v for v in VehiculoVin.query.filter(VehiculoVin.vin.isnot(None)).all() if v.vin
    }

    for idx, raw in enumerate(df.to_dict(orient="records"), start=2):
        mapped = _map_row(raw)
        vin = normalizar_vin(mapped.get("vin"))
        chasis_raw = normalizar_texto(mapped.get("chasis"), 64) or None
        if not vin and not chasis_raw:
            omitidos += 1
            continue

        marca = normalizar_texto(mapped.get("marca"), 80).upper()
        modelo_raw = normalizar_texto(mapped.get("modelo"), 200)
        data = _build_vehicle_data(
            vin=vin,
            chasis=chasis_raw,
            marca=marca,
            modelo_raw=modelo_raw,
            anio_explicit=normalizar_anio(mapped.get("anio")),
            motor=normalizar_texto(mapped.get("motor"), 120).upper(),
            version=normalizar_texto(mapped.get("version"), 120).upper(),
            transmision_explicit=normalizar_texto(mapped.get("transmision"), 80).upper(),
            cilindrada_explicit=normalizar_texto(mapped.get("cilindrada"), 40).upper(),
            patente=normalizar_texto(mapped.get("patente"), 20).upper(),
            nombre_china=normalizar_texto(mapped.get("nombre_china"), 120).upper(),
            notas=normalizar_texto(mapped.get("notas")),
            auto_desglosar=True,
        )

        key = data["vin"] or data["chasis"]
        if not key:
            omitidos += 1
            continue

        try:
            existing = by_vin.get(key) if data["vin"] else None
            if existing is None and data["chasis"]:
                existing = (
                    VehiculoVin.query.filter_by(chasis=data["chasis"]).first()
                )

            if existing:
                _apply_data(existing, data, overwrite_empty=True)
                existing.activo = True
                if not existing.fuente:
                    existing.fuente = "excel"
                existing.usuario_edicion = user
                existing.updated_at = datetime.utcnow()
                actualizados += 1
                if existing.vin:
                    by_vin[existing.vin] = existing
            else:
                v = VehiculoVin(
                    **data,
                    fuente="excel",
                    activo=True,
                    usuario_alta=user,
                    usuario_edicion=user,
                )
                db.session.add(v)
                if data["vin"]:
                    by_vin[data["vin"]] = v
                creados += 1
        except Exception as exc:
            db.session.rollback()
            # Reconstruir caché tras rollback
            by_vin = {
                x.vin: x
                for x in VehiculoVin.query.filter(VehiculoVin.vin.isnot(None)).all()
                if x.vin
            }
            errores.append(f"Fila {idx}: {exc}")
            if len(errores) >= 8:
                break

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        flash(
            "Error al guardar: hay VIN duplicados. Vuelve a importar; el sistema ahora actualiza en lugar de duplicar.",
            "error",
        )
        return redirect(url_for("vehiculos_vin.index"))
    except Exception as exc:
        db.session.rollback()
        flash(f"Error al guardar la importación: {exc}", "error")
        return redirect(url_for("vehiculos_vin.index"))

    msg = f"Importación lista: {creados} nuevos, {actualizados} actualizados"
    if omitidos:
        msg += f", {omitidos} omitidos (sin VIN/chasis)"
    flash(msg + ".", "success")
    if errores:
        flash("Algunas filas fallaron: " + " | ".join(errores[:5]), "error")
    return redirect(url_for("vehiculos_vin.index"))
