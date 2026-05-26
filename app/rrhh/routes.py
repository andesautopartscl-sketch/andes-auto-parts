from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from app.extensions import db
from app.utils.decorators import login_required
from app.utils.permissions import has_permission
from app.utils.csrf import validate_csrf_request
from app.seguridad.models import Rol, Usuario

from .models import (
    RRHHAfpTasa,
    RRHHContratoAnexo,
    RRHHImpuestoTramo,
    RRHHParametrosPeriodo,
    RRHHPerfil,
    RRHHLiquidacion,
    RRHHLiquidacionDetalle,
    RRHHVacacionRegistro,
)


rrhh_bp = Blueprint(
    "rrhh", __name__, url_prefix="/rrhh",
    template_folder="../../templates"
)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_RRHH_UPLOAD_ROOT = _DATA_DIR / "rrhh_uploads"
_MAX_RRHH_PDF_BYTES = 6 * 1024 * 1024


def _resolved_rrhh_file(relpath: str) -> Path | None:
    if not relpath or ".." in relpath or relpath.startswith(("/", "\\")):
        return None
    full = (_DATA_DIR / relpath).resolve()
    try:
        full.relative_to(_DATA_DIR.resolve())
    except ValueError:
        return None
    return full


def _rrhh_data_path(relpath: str) -> Path:
    p = _resolved_rrhh_file(relpath)
    if p is None or not p.is_file():
        abort(404)
    return p


def _unlink_rrhh_file(relpath: str | None) -> None:
    if not relpath:
        return
    p = _resolved_rrhh_file(relpath)
    if p and p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


def _save_rrhh_pdf(file_storage, usuario_id: int, name_prefix: str) -> tuple[str, str]:
    if not file_storage or not getattr(file_storage, "filename", None):
        raise ValueError("No se recibió archivo")
    orig = secure_filename(file_storage.filename) or "documento.pdf"
    if Path(orig).suffix.lower() != ".pdf":
        raise ValueError("Solo se permiten archivos PDF")
    uid = int(usuario_id)
    folder = _RRHH_UPLOAD_ROOT / f"u{uid}"
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = f"{name_prefix}_{secrets.token_hex(6)}.pdf"
    relpath = f"rrhh_uploads/u{uid}/{safe_name}"
    dest = _DATA_DIR / relpath
    file_storage.save(str(dest))
    if dest.stat().st_size > _MAX_RRHH_PDF_BYTES:
        dest.unlink(missing_ok=True)
        raise ValueError("El PDF supera el tamaño máximo (6 MB)")
    return relpath, orig


@rrhh_bp.before_request
def _rrhh_module_guard():
    if "user" not in session:
        return None
    path = request.path or ""
    if path.startswith("/rrhh/mi-expediente") or path.startswith("/rrhh/archivo/"):
        return None
    if has_permission(session.get("user"), session.get("rol"), "mod_rrhh"):
        return None
    is_ajax = request.is_json or (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest"
    if is_ajax or path.startswith("/rrhh/api/"):
        return jsonify({"ok": False, "error": "Permiso denegado para módulo RRHH"}), 403
    flash("No tienes permisos para acceder al módulo RRHH.", "error")
    return redirect(url_for("productos.buscar"))


def _periodo_default() -> str:
    d = date.today()
    return f"{d.year:04d}-{d.month:02d}"


def _antiguedad_en_empresa(fecha_alta: date | datetime | None) -> str:
    """Texto legible desde fecha de alta (p. ej. fecha_creacion del usuario)."""
    if not fecha_alta:
        return "—"
    if isinstance(fecha_alta, datetime):
        d0 = fecha_alta.date()
    else:
        d0 = fecha_alta
    today = date.today()
    if d0 > today:
        return "—"
    total_m = (today.year - d0.year) * 12 + (today.month - d0.month)
    if today.day < d0.day:
        total_m -= 1
    if total_m < 0:
        return "—"
    years, months = divmod(total_m, 12)
    parts: list[str] = []
    if years:
        parts.append(f"{years} año{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} mes{'es' if months != 1 else ''}")
    if not parts:
        return "Menos de un mes"
    return " y ".join(parts)


def _usuario_card(u: Usuario) -> dict:
    from app.utils.user_photo import user_has_photo, user_photo_url

    return {
        "id": u.id,
        "nombre": u.nombre or "",
        "usuario": u.usuario or "",
        "fecha_nacimiento": u.fecha_nacimiento,
        "antiguedad": _antiguedad_en_empresa(u.fecha_creacion),
        "fecha_alta_fmt": u.fecha_creacion.strftime("%d/%m/%Y") if u.fecha_creacion else None,
        "foto_url": user_photo_url(u),
        "has_foto": user_has_photo(u),
    }


@rrhh_bp.route("/", methods=["GET"])
@login_required
def index():
    periodo = (request.args.get("periodo") or "").strip() or _periodo_default()
    return redirect(url_for("rrhh.periodo", periodo=periodo))


@rrhh_bp.route("/periodo/<periodo>", methods=["GET"])
@login_required
def periodo(periodo: str):
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("No tienes permiso para ver RRHH/Nómina.", "error")
        return redirect(url_for("productos.buscar"))

    periodo = (periodo or "").strip()[:7]
    # Vendors: users with profile es_vendedor and >0 comision_pct
    perfiles = (
        db.session.query(RRHHPerfil, Usuario)
        .join(Usuario, Usuario.id == RRHHPerfil.usuario_id)
        .filter(RRHHPerfil.es_vendedor.is_(True))
        .order_by(Usuario.nombre.asc())
        .all()
    )

    # Existing liquidaciones for this period
    liquidaciones = (
        RRHHLiquidacion.query
        .filter_by(periodo=periodo)
        .all()
    )
    liq_by_uid = {l.usuario_id: l for l in liquidaciones}

    return render_template(
        "rrhh/periodo.html",
        active_page="rrhh",
        periodo=periodo,
        perfiles=perfiles,
        liq_by_uid=liq_by_uid,
        _partial=_partial,
    )


@rrhh_bp.route("/organigrama", methods=["GET"])
@login_required
def organigrama():
    """Vista tipo organigrama: usuarios activos agrupados por rol, con nombre y cumpleaños."""
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("No tienes permiso para ver el organigrama de equipo.", "error")
        return redirect(url_for("productos.buscar"))

    rows = (
        db.session.query(Usuario, Rol)
        .outerjoin(Rol, Rol.id == Usuario.rol_id)
        .filter(Usuario.activo.is_(True))
        .order_by(Usuario.nombre.asc())
        .all()
    )
    by_rol: dict[str, list[Usuario]] = defaultdict(list)
    nivel_por_rol: dict[str, int | None] = {}
    for u, r in rows:
        label = (r.nombre or "").strip() if r else ""
        if not label:
            label = "Sin rol"
        by_rol[label].append(u)
        if label not in nivel_por_rol:
            nivel_por_rol[label] = r.nivel if r else None

    def _sort_key(label: str) -> tuple:
        n = nivel_por_rol.get(label)
        n_val = n if n is not None else -1
        return (-n_val, label.lower())

    groups = [
        {
            "rol": label,
            "nivel": nivel_por_rol.get(label),
            "usuarios": [_usuario_card(u) for u in sorted(users, key=lambda x: (x.nombre or "").lower())],
        }
        for label, users in sorted(by_rol.items(), key=lambda kv: _sort_key(kv[0]))
    ]

    niveles = [g["nivel"] for g in groups if g["nivel"] is not None]
    max_n = max(niveles) if niveles else None
    if max_n is None:
        top_groups = groups
        bottom_groups: list[dict] = []
    else:
        top_groups = [g for g in groups if g["nivel"] == max_n]
        bottom_groups = [g for g in groups if g["nivel"] != max_n]

    return render_template(
        "rrhh/organigrama.html",
        active_page="rrhh_organigrama",
        top_groups=top_groups,
        bottom_groups=bottom_groups,
        _partial=_partial,
    )


@rrhh_bp.route("/empleado/<int:uid>", methods=["GET", "POST"])
@login_required
def empleado_expediente(uid: int):
    """Expediente RRHH: liquidaciones, vacaciones y notas de contrato por persona."""
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("No tienes permiso para ver expedientes RRHH.", "error")
        return redirect(url_for("productos.buscar"))

    u = (
        Usuario.query.options(joinedload(Usuario.rol))
        .filter(Usuario.id == uid, Usuario.activo.is_(True))
        .first()
    )
    if not u:
        flash("Usuario no encontrado o inactivo.", "error")
        return redirect(url_for("rrhh.organigrama"))

    perfil = RRHHPerfil.query.filter_by(usuario_id=uid).first()
    if perfil is None:
        perfil = RRHHPerfil(usuario_id=uid)
        db.session.add(perfil)
        db.session.commit()

    puede_editar = has_permission(session.get("user"), session.get("rol"), "rrhh_editar")

    if request.method == "POST":
        if not puede_editar:
            flash("No tienes permiso para editar expedientes.", "error")
            return redirect(url_for("rrhh.empleado_expediente", uid=uid))
        if not validate_csrf_request():
            flash("Sesión de seguridad expirada. Vuelve a intentar.", "error")
            return redirect(url_for("rrhh.empleado_expediente", uid=uid))

        action = (request.form.get("action") or "").strip()
        try:
            if action == "save_contrato":
                raw = (request.form.get("contrato_vigencia_desde") or "").strip()
                perfil.contrato_vigencia_desde = date.fromisoformat(raw) if raw else None
                perfil.contrato_notas = (request.form.get("contrato_notas") or "").strip()[:500]
                db.session.commit()
                flash("Datos de contrato guardados.", "success")

            elif action == "upload_contrato_pdf":
                f = request.files.get("contrato_pdf")
                if not f or not getattr(f, "filename", None):
                    raise ValueError("Selecciona un archivo PDF.")
                _unlink_rrhh_file(perfil.contrato_pdf_relpath)
                relp, orig = _save_rrhh_pdf(f, uid, "contrato")
                perfil.contrato_pdf_relpath = relp
                perfil.contrato_pdf_original = orig[:260]
                db.session.commit()
                flash("Contrato PDF actualizado. El trabajador solo podrá verlo (no editarlo).", "success")

            elif action == "remove_contrato_pdf":
                _unlink_rrhh_file(perfil.contrato_pdf_relpath)
                perfil.contrato_pdf_relpath = None
                perfil.contrato_pdf_original = None
                db.session.commit()
                flash("Archivo de contrato eliminado.", "success")

            elif action == "add_anexo":
                f = request.files.get("anexo_pdf")
                if not f or not getattr(f, "filename", None):
                    raise ValueError("Adjunta un PDF para el anexo.")
                titulo = (request.form.get("anexo_titulo") or "").strip()[:200]
                mensaje = (request.form.get("anexo_mensaje") or "").strip()[:500]
                if not titulo:
                    raise ValueError("Indica un título para el anexo.")
                relp, orig = _save_rrhh_pdf(f, uid, "anexo")
                sid = session.get("usuario_id")
                db.session.add(RRHHContratoAnexo(
                    usuario_id=uid,
                    titulo=titulo,
                    archivo_relpath=relp,
                    nombre_original=orig[:260],
                    mensaje=mensaje,
                    estado="pendiente",
                    creado_por_usuario_id=int(sid) if sid else None,
                ))
                db.session.commit()
                flash("Anexo registrado. El trabajador verá la notificación en Mi expediente laboral.", "success")

            elif action == "delete_anexo":
                aid = int(request.form.get("anexo_id") or 0)
                row = db.session.get(RRHHContratoAnexo, aid)
                if row and row.usuario_id == uid:
                    _unlink_rrhh_file(row.archivo_relpath)
                    db.session.delete(row)
                    db.session.commit()
                    flash("Anexo eliminado.", "success")

            elif action == "add_vacacion":
                tipo = (request.form.get("vac_tipo") or "").strip().lower()
                if tipo not in ("solicitud", "tomada"):
                    tipo = "solicitud"
                ini_s = (request.form.get("vac_fecha_inicio") or "").strip()
                fin_s = (request.form.get("vac_fecha_fin") or "").strip()
                if not ini_s:
                    raise ValueError("Indica la fecha de inicio.")
                fecha_ini = date.fromisoformat(ini_s)
                fecha_fin = date.fromisoformat(fin_s) if fin_s else None
                dias_raw = (request.form.get("vac_dias") or "").strip()
                dias = int(dias_raw) if dias_raw else None
                estado = (request.form.get("vac_estado") or "").strip()[:24] if tipo == "solicitud" else ""
                notas = (request.form.get("vac_notas") or "").strip()[:500]
                db.session.add(RRHHVacacionRegistro(
                    usuario_id=uid,
                    tipo=tipo,
                    fecha_inicio=fecha_ini,
                    fecha_fin=fecha_fin,
                    dias=dias,
                    estado=estado or ("pendiente" if tipo == "solicitud" else ""),
                    notas=notas,
                ))
                db.session.commit()
                flash("Registro de vacaciones agregado.", "success")

            elif action == "delete_vacacion":
                vid = int(request.form.get("vac_id") or 0)
                row = db.session.get(RRHHVacacionRegistro, vid)
                if row and row.usuario_id == uid:
                    db.session.delete(row)
                    db.session.commit()
                    flash("Registro eliminado.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"No se pudo guardar: {exc}", "error")

        return redirect(url_for("rrhh.empleado_expediente", uid=uid))

    liquidaciones = (
        RRHHLiquidacion.query
        .filter_by(usuario_id=uid)
        .order_by(RRHHLiquidacion.periodo.desc())
        .all()
    )
    vac_rows = (
        RRHHVacacionRegistro.query
        .filter_by(usuario_id=uid)
        .order_by(RRHHVacacionRegistro.fecha_inicio.desc(), RRHHVacacionRegistro.id.desc())
        .all()
    )
    solicitudes = [v for v in vac_rows if (v.tipo or "").lower() == "solicitud"]
    tomadas = [v for v in vac_rows if (v.tipo or "").lower() == "tomada"]

    anexos = (
        RRHHContratoAnexo.query
        .filter_by(usuario_id=uid)
        .order_by(RRHHContratoAnexo.creado_at.desc())
        .all()
    )

    return render_template(
        "rrhh/empleado_expediente.html",
        active_page="rrhh_expediente",
        u=u,
        perfil=perfil,
        liquidaciones=liquidaciones,
        solicitudes=solicitudes,
        tomadas=tomadas,
        anexos=anexos,
        puede_editar=puede_editar,
        _partial=_partial,
    )


@rrhh_bp.route("/mi-expediente", methods=["GET", "POST"])
@login_required
def mi_expediente():
    """Vista del propio trabajador: contrato y anexos (solo lectura + aceptación)."""
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    uid = session.get("usuario_id")
    if not uid:
        flash("Sesión inválida.", "error")
        return redirect(url_for("auth.login"))

    u = Usuario.query.options(joinedload(Usuario.rol)).filter(Usuario.id == int(uid), Usuario.activo.is_(True)).first()
    if not u:
        flash("Usuario no encontrado.", "error")
        return redirect(url_for("productos.buscar"))

    perfil = RRHHPerfil.query.filter_by(usuario_id=u.id).first()
    if perfil is None:
        perfil = RRHHPerfil(usuario_id=u.id)
        db.session.add(perfil)
        db.session.commit()

    if request.method == "POST":
        if not validate_csrf_request():
            flash("Sesión de seguridad expirada.", "error")
            return redirect(url_for("rrhh.mi_expediente"))
        action = (request.form.get("action") or "").strip()
        if action == "aceptar_anexo":
            aid = int(request.form.get("anexo_id") or 0)
            row = db.session.get(RRHHContratoAnexo, aid)
            if not row or row.usuario_id != int(uid):
                flash("Anexo no válido.", "error")
            elif (row.estado or "").lower() == "aceptado":
                flash("Este anexo ya fue aceptado.", "success")
            elif not request.form.get("declaracion"):
                flash("Debes marcar la declaración de aceptación electrónica.", "error")
            else:
                row.estado = "aceptado"
                row.aceptado_at = datetime.utcnow()
                raw = "|".join(
                    [
                        str(request.remote_addr or ""),
                        str(request.headers.get("User-Agent") or ""),
                        str(uid),
                        str(row.id),
                        row.aceptado_at.isoformat() if row.aceptado_at else "",
                    ]
                )
                row.aceptado_evidencia_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                db.session.commit()
                flash("Anexo aceptado. Quedó registrada tu conformidad.", "success")
        return redirect(url_for("rrhh.mi_expediente"))

    anexos = (
        RRHHContratoAnexo.query
        .filter_by(usuario_id=u.id)
        .order_by(RRHHContratoAnexo.creado_at.desc())
        .all()
    )
    pendientes = [a for a in anexos if (a.estado or "").lower() != "aceptado"]
    aceptados = [a for a in anexos if (a.estado or "").lower() == "aceptado"]

    return render_template(
        "rrhh/mi_expediente.html",
        active_page="rrhh_mi_expediente",
        u=u,
        perfil=perfil,
        anexos_pendientes=pendientes,
        anexos_aceptados=aceptados,
        _partial=_partial,
    )


@rrhh_bp.route("/archivo/contrato/<int:uid>")
@login_required
def rrhh_archivo_contrato(uid: int):
    me = session.get("usuario_id")
    if me is None:
        abort(403)
    if int(me) != int(uid) and not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("No tienes permiso para ver este archivo.", "error")
        return redirect(url_for("productos.buscar"))
    perfil = RRHHPerfil.query.filter_by(usuario_id=uid).first()
    if not perfil or not perfil.contrato_pdf_relpath:
        abort(404)
    path = _rrhh_data_path(perfil.contrato_pdf_relpath)
    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=(perfil.contrato_pdf_original or "contrato.pdf").replace("/", "_"),
    )


@rrhh_bp.route("/archivo/anexo/<int:aid>")
@login_required
def rrhh_archivo_anexo(aid: int):
    me = session.get("usuario_id")
    if me is None:
        abort(403)
    row = db.session.get(RRHHContratoAnexo, aid)
    if not row:
        abort(404)
    if int(me) != int(row.usuario_id) and not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("No tienes permiso para ver este archivo.", "error")
        return redirect(url_for("productos.buscar"))
    path = _rrhh_data_path(row.archivo_relpath)
    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=(row.nombre_original or "").replace("/", "_") or f"anexo_{aid}.pdf",
    )


@rrhh_bp.route("/config", methods=["GET", "POST"])
@login_required
def config():
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_editar"):
        flash("No tienes permiso para configurar RRHH/Nómina.", "error")
        return redirect(url_for("rrhh.index"))

    periodo = (request.args.get("periodo") or "").strip()[:7] or _periodo_default()
    params = _get_parametros_periodo(periodo)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        try:
            if action == "save_periodo":
                def _num(name: str, default: float) -> float:
                    raw = str(request.form.get(name) or "").strip().replace(",", ".")
                    try:
                        return float(raw)
                    except Exception:
                        return default

                params.fonasa_tasa_pct = _num("fonasa_tasa_pct", float(params.fonasa_tasa_pct or 7.0))
                params.isapre_tasa_pct = _num("isapre_tasa_pct", float(params.isapre_tasa_pct or 7.0))
                params.afc_trabajador_tasa_pct = _num("afc_trabajador_tasa_pct", float(params.afc_trabajador_tasa_pct or 0.6))
                db.session.commit()
                flash("Parámetros del período guardados.", "success")
                return redirect(url_for("rrhh.config", periodo=periodo))

            if action == "add_afp":
                nombre = (request.form.get("afp_nombre") or "").strip()
                tasa = str(request.form.get("afp_tasa_pct") or "").strip().replace(",", ".")
                if not nombre:
                    raise ValueError("Nombre AFP requerido")
                try:
                    tasa_f = float(tasa)
                except Exception:
                    tasa_f = 0.0
                row = RRHHAfpTasa.query.filter(func.lower(RRHHAfpTasa.nombre) == nombre.lower()).first()
                if row is None:
                    row = RRHHAfpTasa(nombre=nombre)
                    db.session.add(row)
                row.tasa_pct = tasa_f
                db.session.commit()
                flash("AFP guardada.", "success")
                return redirect(url_for("rrhh.config", periodo=periodo))

            if action == "add_tramo":
                desde = int(request.form.get("tramo_desde") or 0)
                hasta_raw = str(request.form.get("tramo_hasta") or "").strip()
                hasta = int(hasta_raw) if hasta_raw else None
                tasa = float(str(request.form.get("tramo_tasa_pct") or "0").replace(",", "."))
                rebaja = int(request.form.get("tramo_rebaja") or 0)
                vigente_desde_str = (request.form.get("vigente_desde") or "").strip()
                vdate = date.fromisoformat(vigente_desde_str) if vigente_desde_str else date.today()
                row = RRHHImpuestoTramo(
                    vigente_desde=vdate,
                    desde=desde,
                    hasta=hasta,
                    tasa_pct=tasa,
                    rebaja=rebaja,
                )
                db.session.add(row)
                db.session.commit()
                flash("Tramo agregado.", "success")
                return redirect(url_for("rrhh.config", periodo=periodo))

        except Exception as exc:
            db.session.rollback()
            flash(f"No se pudo guardar: {exc}", "error")

    afps = RRHHAfpTasa.query.order_by(RRHHAfpTasa.nombre.asc()).all()
    tramos = RRHHImpuestoTramo.query.order_by(RRHHImpuestoTramo.vigente_desde.desc(), RRHHImpuestoTramo.desde.asc()).all()
    return render_template(
        "rrhh/config.html",
        active_page="rrhh",
        periodo=periodo,
        params=params,
        afps=afps,
        tramos=tramos,
        _partial=_partial,
    )


@rrhh_bp.route("/api/periodo/<periodo>/precalcular_comision", methods=["POST"])
@login_required
def api_precalcular_comision(periodo: str):
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        return jsonify({"ok": False, "error": "Sin permiso"}), 403
    from app.ventas.models import DocumentoVenta, NotaCredito

    periodo = (periodo or "").strip()[:7]
    uid = int((request.get_json(force=True) or {}).get("usuario_id") or 0)
    perfil = RRHHPerfil.query.filter_by(usuario_id=uid).first()
    user = db.session.get(Usuario, uid)
    if not perfil or not user:
        return jsonify({"ok": False, "error": "Vendedor no encontrado"}), 404

    username = (user.usuario or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Usuario sin username"}), 400

    y, m = periodo.split("-")
    year = int(y)
    month = int(m)

    docs_total = (
        db.session.query(func.sum(DocumentoVenta.total))
        .filter(
            DocumentoVenta.tipo.in_(["factura", "boleta"]),
            DocumentoVenta.status != "anulada",
            DocumentoVenta.usuario == username,
            func.strftime("%Y", DocumentoVenta.fecha_documento) == f"{year:04d}",
            func.strftime("%m", DocumentoVenta.fecha_documento) == f"{month:02d}",
        )
        .scalar()
    ) or 0.0

    # Credit notes: subtract immediately by link to docs of this seller.
    nc_total = (
        db.session.query(func.sum(NotaCredito.total))
        .join(DocumentoVenta, NotaCredito.documento_venta_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.usuario == username,
            DocumentoVenta.tipo.in_(["factura", "boleta"]),
            DocumentoVenta.status != "anulada",
            func.strftime("%Y", NotaCredito.fecha_documento) == f"{year:04d}",
            func.strftime("%m", NotaCredito.fecha_documento) == f"{month:02d}",
        )
        .scalar()
    ) or 0.0

    bruto = float(docs_total) - float(nc_total)
    pct = float(perfil.comision_pct or 0)
    comision = round(bruto * (pct / 100.0))
    return jsonify({
        "ok": True,
        "ventas_total": round(float(docs_total)),
        "notas_credito_total": round(float(nc_total)),
        "base_comision": round(bruto),
        "comision_pct": pct,
        "comision_calculada": int(comision),
    })


def _get_parametros_periodo(periodo: str) -> RRHHParametrosPeriodo:
    row = RRHHParametrosPeriodo.query.filter_by(periodo=periodo).first()
    if row:
        return row
    row = RRHHParametrosPeriodo(periodo=periodo)
    db.session.add(row)
    db.session.flush()
    return row


def _afp_rate_pct(afp_nombre: str) -> float:
    n = (afp_nombre or "").strip()
    if not n:
        return 0.0
    row = RRHHAfpTasa.query.filter(func.lower(RRHHAfpTasa.nombre) == n.lower()).first()
    return float(row.tasa_pct or 0) if row else 0.0


def _impuesto_unico(base_imponible: int, vigente_al: date | None = None) -> int:
    vdate = vigente_al or date.today()
    # Pick tramos with vigente_desde <= vdate; among them choose matching range.
    tramos = (
        RRHHImpuestoTramo.query
        .filter(RRHHImpuestoTramo.vigente_desde <= vdate)
        .order_by(RRHHImpuestoTramo.vigente_desde.desc(), RRHHImpuestoTramo.desde.asc())
        .all()
    )
    if not tramos:
        return 0
    # Use latest vigente_desde set
    latest = tramos[0].vigente_desde
    applicable = [t for t in tramos if t.vigente_desde == latest]
    for t in applicable:
        desde = int(t.desde or 0)
        hasta = int(t.hasta) if t.hasta is not None else None
        if base_imponible < desde:
            continue
        if hasta is None or base_imponible <= hasta:
            tasa = float(t.tasa_pct or 0) / 100.0
            rebaja = int(t.rebaja or 0)
            return max(0, round(base_imponible * tasa) - rebaja)
    return 0


def _recompute_liquidacion(liq: RRHHLiquidacion, perfil: RRHHPerfil) -> None:
    """Recalcula base imponible, descuentos e impuesto en base a campos actuales."""
    base_imponible = max(
        0,
        int(liq.sueldo_base or 0)
        + int(liq.comision_bruta or 0)
        + int(liq.haberes_otros or 0)
        - int(liq.descuentos_otros or 0),
    )
    liq.base_imponible = base_imponible

    params = _get_parametros_periodo(liq.periodo)
    salud_rate_pct = (
        float(params.fonasa_tasa_pct or 0)
        if (perfil.salud_tipo or "").strip().upper() == "FONASA"
        else float(params.isapre_tasa_pct or 0)
    )
    liq.salud_descuento = int(round(base_imponible * (salud_rate_pct / 100.0)))

    afp_rate_pct = _afp_rate_pct(perfil.afp_nombre)
    liq.afp_descuento = int(round(base_imponible * (afp_rate_pct / 100.0))) if afp_rate_pct > 0 else 0

    liq.afc_descuento = 0
    if bool(perfil.afc_afiliado):
        afc_rate_pct = float(params.afc_trabajador_tasa_pct or 0)
        liq.afc_descuento = int(round(base_imponible * (afc_rate_pct / 100.0)))

    liq.impuesto_unico = _impuesto_unico(base_imponible)
    liq.total_liquido = (
        int(base_imponible)
        - int(liq.salud_descuento or 0)
        - int(liq.afp_descuento or 0)
        - int(liq.afc_descuento or 0)
        - int(liq.impuesto_unico or 0)
    )


@rrhh_bp.route("/api/liquidacion/generar", methods=["POST"])
@login_required
def api_generar_liquidacion():
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_editar"):
        return jsonify({"ok": False, "error": "Sin permiso para generar liquidaciones"}), 403

    from app.ventas.models import DocumentoVenta, NotaCredito

    data = request.get_json(force=True) or {}
    periodo = (data.get("periodo") or "").strip()[:7] or _periodo_default()
    uid = int(data.get("usuario_id") or 0)
    sueldo_base = int(data.get("sueldo_base") or 0)
    haberes_otros = int(data.get("haberes_otros") or 0)
    descuentos_otros = int(data.get("descuentos_otros") or 0)

    perfil = RRHHPerfil.query.filter_by(usuario_id=uid).first()
    user = db.session.get(Usuario, uid)
    if not perfil or not user:
        return jsonify({"ok": False, "error": "Usuario no encontrado"}), 404

    username = (user.usuario or "").strip()
    y, m = periodo.split("-")
    year = int(y)
    month = int(m)

    docs_total = (
        db.session.query(func.sum(DocumentoVenta.total))
        .filter(
            DocumentoVenta.tipo.in_(["factura", "boleta"]),
            DocumentoVenta.status != "anulada",
            DocumentoVenta.usuario == username,
            func.strftime("%Y", DocumentoVenta.fecha_documento) == f"{year:04d}",
            func.strftime("%m", DocumentoVenta.fecha_documento) == f"{month:02d}",
        )
        .scalar()
    ) or 0.0
    nc_total = (
        db.session.query(func.sum(NotaCredito.total))
        .join(DocumentoVenta, NotaCredito.documento_venta_id == DocumentoVenta.id)
        .filter(
            DocumentoVenta.usuario == username,
            DocumentoVenta.tipo.in_(["factura", "boleta"]),
            DocumentoVenta.status != "anulada",
            func.strftime("%Y", NotaCredito.fecha_documento) == f"{year:04d}",
            func.strftime("%m", NotaCredito.fecha_documento) == f"{month:02d}",
        )
        .scalar()
    ) or 0.0

    base_venta = float(docs_total) - float(nc_total)
    comision_bruta = int(round(base_venta * (float(perfil.comision_pct or 0) / 100.0)))

    liq = RRHHLiquidacion.query.filter_by(usuario_id=uid, periodo=periodo).first()
    creating = liq is None
    if liq is None:
        liq = RRHHLiquidacion(usuario_id=uid, periodo=periodo)
        db.session.add(liq)

    if liq.estado not in ("borrador",):
        return jsonify({"ok": False, "error": "La liquidación no está en borrador"}), 409

    liq.sueldo_base = sueldo_base
    liq.comision_bruta = comision_bruta
    liq.haberes_otros = haberes_otros
    liq.descuentos_otros = descuentos_otros
    _recompute_liquidacion(liq, perfil)

    # Replace details for commission snapshot.
    if not creating:
        RRHHLiquidacionDetalle.query.filter_by(liquidacion_id=liq.id).delete()
    db.session.flush()
    db.session.add(RRHHLiquidacionDetalle(
        liquidacion_id=liq.id,
        tipo="comision",
        referencia=f"ventas:{periodo}",
        descripcion="Comisión mensual (ventas - NC)",
        monto=comision_bruta,
    ))
    if float(nc_total) > 0:
        db.session.add(RRHHLiquidacionDetalle(
            liquidacion_id=liq.id,
            tipo="ajuste",
            referencia=f"nc:{periodo}",
            descripcion="Ajuste por notas de crédito del periodo",
            monto=-int(round(float(nc_total))),
        ))

    db.session.commit()
    return jsonify({"ok": True, "liquidacion_id": liq.id})


@rrhh_bp.route("/liquidacion/<int:lid>", methods=["GET"])
@login_required
def liquidacion_detalle(lid: int):
    _partial = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_ver"):
        flash("Sin permiso para ver liquidación.", "error")
        return redirect(url_for("rrhh.index"))
    liq = db.session.get(RRHHLiquidacion, lid)
    if not liq:
        flash("Liquidación no encontrada.", "error")
        return redirect(url_for("rrhh.index"))
    user = db.session.get(Usuario, liq.usuario_id)
    perfil = RRHHPerfil.query.filter_by(usuario_id=liq.usuario_id).first()
    detalles = RRHHLiquidacionDetalle.query.filter_by(liquidacion_id=liq.id).order_by(RRHHLiquidacionDetalle.id.asc()).all()
    return render_template(
        "rrhh/liquidacion_detalle.html",
        active_page="rrhh",
        liq=liq,
        u=user,
        perfil=perfil,
        detalles=detalles,
        _partial=_partial,
    )


@rrhh_bp.route("/api/liquidacion/<int:lid>/detalle", methods=["POST"])
@login_required
def api_agregar_detalle(lid: int):
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_editar"):
        return jsonify({"ok": False, "error": "Sin permiso"}), 403
    liq = db.session.get(RRHHLiquidacion, lid)
    if not liq:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    if liq.estado != "borrador":
        return jsonify({"ok": False, "error": "Solo en borrador"}), 409
    data = request.get_json(force=True) or {}
    tipo = (data.get("tipo") or "ajuste").strip().lower()
    descripcion = (data.get("descripcion") or "").strip()[:255]
    referencia = (data.get("referencia") or "").strip()[:120]
    try:
        monto = int(float(str(data.get("monto") or "0").replace(",", ".")))
    except Exception:
        monto = 0
    if tipo not in {"ajuste", "haber", "descuento"}:
        tipo = "ajuste"
    if tipo == "descuento" and monto > 0:
        monto = -monto
    if tipo == "haber" and monto < 0:
        monto = -monto

    db.session.add(RRHHLiquidacionDetalle(
        liquidacion_id=liq.id,
        tipo=tipo,
        referencia=referencia,
        descripcion=descripcion,
        monto=monto,
    ))

    # Recompute totals: aggregate manual lines into haberes/descuentos.
    rows = RRHHLiquidacionDetalle.query.filter_by(liquidacion_id=liq.id).all()
    haberes = 0
    descuentos = 0
    for r in rows:
        if (r.tipo or "").lower() in {"haber"}:
            haberes += int(r.monto or 0)
        elif (r.tipo or "").lower() in {"descuento"}:
            descuentos += abs(int(r.monto or 0))
    liq.haberes_otros = haberes
    liq.descuentos_otros = descuentos

    perfil = RRHHPerfil.query.filter_by(usuario_id=liq.usuario_id).first() or RRHHPerfil(usuario_id=liq.usuario_id)
    _recompute_liquidacion(liq, perfil)
    db.session.commit()
    return jsonify({"ok": True})


@rrhh_bp.route("/api/liquidacion/<int:lid>/cerrar", methods=["POST"])
@login_required
def api_cerrar_liquidacion(lid: int):
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_editar"):
        return jsonify({"ok": False, "error": "Sin permiso"}), 403
    liq = db.session.get(RRHHLiquidacion, lid)
    if not liq:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    if liq.estado != "borrador":
        return jsonify({"ok": False, "error": "Estado inválido"}), 409
    liq.estado = "cerrada"
    db.session.commit()
    return jsonify({"ok": True})


@rrhh_bp.route("/api/liquidacion/<int:lid>/pagar", methods=["POST"])
@login_required
def api_pagar_liquidacion(lid: int):
    if not has_permission(session.get("user"), session.get("rol"), "rrhh_pagar"):
        return jsonify({"ok": False, "error": "Sin permiso para pagar"}), 403
    liq = db.session.get(RRHHLiquidacion, lid)
    if not liq:
        return jsonify({"ok": False, "error": "No encontrada"}), 404
    if liq.estado != "cerrada":
        return jsonify({"ok": False, "error": "Debe estar cerrada antes de pagar"}), 409
    data = request.get_json(force=True) or {}
    medio = (data.get("pago_medio") or "").strip()[:40]
    ref = (data.get("pago_referencia") or "").strip()[:120]
    liq.pago_medio = medio
    liq.pago_referencia = ref
    liq.pago_fecha = date.today()
    liq.estado = "pagada"
    db.session.commit()
    return jsonify({"ok": True})

