#!/usr/bin/env python3
"""
Restablece correlativos de cotizaciones (CO), órdenes de venta (OV), facturas (FA) y boletas (BO).

La numeración siguiente se calcula con max(número) + 1; por tanto, para que el próximo documento
sea CO-0001, OV-0001, FA-0001 o BO-0001 no puede quedar ningún registro previo de ese prefijo.

Este script ELIMINA todos los documentos ventas_documentos con tipo en:
  cotizacion, orden_venta, factura, boleta

Antes de borrar facturas/boletas que ya descontaron stock, devuelve el inventario (misma lógica
que una nota de crédito) para no dejar bodega inconsistente.

No elimina: clientes, proveedores, órdenes de compra, productos, movimientos históricos ya
registrados (salvo la reversión explícita de líneas del documento).

Uso:
  python scripts/reset_ventas_correlativos.py --dry-run
  python scripts/reset_ventas_correlativos.py --yes
  python scripts/reset_ventas_correlativos.py --yes --ignore-stock
    (solo borra registros; NO ajusta bodega — use si hubo error de stock o para cuadrar después a mano)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Raíz del proyecto (scripts/ -> repo root)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import session
from sqlalchemy import or_

from app import create_app
from app.extensions import db
from app.postventa.models import Garantia
from app.bodega.models import PickingVenta
from app.ventas.models import DocumentoVenta, NotaCredito
from app.ventas.routes import _adjust_product_stock, _safe_int

TIPOS_RESET = ("cotizacion", "orden_venta", "factura", "boleta")


def _undo_nota_credito_stock(nc: NotaCredito) -> None:
    """Deshace el ingreso de stock que aplicó la NC (antes de borrar la factura asociada)."""
    if not nc.stock_restored:
        return
    for item in nc.items:
        qty = _safe_int(str(item.cantidad or 0), default=0)
        if qty <= 0:
            continue
        err = _adjust_product_stock(
            codigo=(item.codigo_producto or "").strip().upper(),
            marca=(item.marca or "").strip().upper(),
            bodega=(item.bodega or "").strip() or "Bodega 1",
            delta=-qty,
            reason=f"Reset correlativos (revierte NC {nc.numero or nc.id})",
        )
        if err:
            raise RuntimeError(err)


def _reverse_stock_for_factura_boleta(doc: DocumentoVenta) -> None:
    t = (doc.tipo or "").strip().lower()
    if t not in ("factura", "boleta"):
        return
    if not doc.stock_deducted:
        return
    for item in doc.items:
        qty = _safe_int(str(item.cantidad or 0), default=0)
        if qty <= 0:
            continue
        err = _adjust_product_stock(
            codigo=(item.codigo_producto or "").strip().upper(),
            marca=(item.marca or "").strip().upper(),
            bodega=(item.bodega or "").strip() or "Bodega 1",
            delta=qty,
            reason=f"Reset correlativos ventas (reversa doc {doc.numero or doc.id})",
        )
        if err:
            raise RuntimeError(err)


def _run(*, dry_run: bool, confirm: bool, ignore_stock: bool) -> int:
    app = create_app()
    with app.app_context():
        q = DocumentoVenta.query.filter(DocumentoVenta.tipo.in_(TIPOS_RESET))
        docs = q.order_by(DocumentoVenta.id.asc()).all()
        ids = {d.id for d in docs}

        if not ids:
            print("No hay documentos de tipo CO/OV/FA/BO. Nada que hacer.")
            return 0

        n_nc = NotaCredito.query.filter(NotaCredito.documento_venta_id.in_(ids)).count()
        n_pick = PickingVenta.query.filter(PickingVenta.orden_venta_id.in_(ids)).count()
        n_stock = sum(1 for d in docs if (d.tipo or "").lower() in ("factura", "boleta") and d.stock_deducted)

        print(f"Documentos a eliminar: {len(ids)}")
        print(f"  Notas de crédito ligadas: {n_nc}")
        print(f"  Pickings de OV ligados: {n_pick}")
        print(f"  Facturas/boletas con stock ya descontado (se revertirá antes de borrar): {n_stock}")
        if ignore_stock:
            print("\n  AVISO: --ignore-stock activo: se eliminarán documentos SIN revertir movimientos de inventario.")
            print("         Revise stock en bodega / productos si tenía ventas con descuento de stock.")

        if dry_run:
            print("\n[--dry-run] No se modificó la base de datos.")
            return 0

        if not confirm:
            print("\nEjecute con --yes para confirmar (o use --dry-run para simular).")
            return 2

        if ignore_stock:
            _execute_reset(ids, docs, adjust_stock=False)
        else:
            try:
                # _adjust_product_stock usa flask.session (usuario en movimientos_stock)
                with app.test_request_context("/"):
                    session["user"] = "reset_correlativos"
                    session.modified = True
                    _execute_reset(ids, docs, adjust_stock=True)
            except RuntimeError as exc:
                print(f"\nError al ajustar stock: {exc}")
                print("Puede ejecutar de nuevo con --ignore-stock para borrar solo los documentos (cuadre manual de inventario).")
                db.session.rollback()
                return 1

        print("\nListo. Los próximos números sugeridos serán CO-0001, OV-0001, FA-0001, BO-0001 (si no hay otros registros con esos prefijos).")
        return 0


def _execute_reset(ids: set[int], docs: list[DocumentoVenta], *, adjust_stock: bool) -> None:
    """Ajusta stock opcionalmente; luego borra NC, pickings y documentos."""
    # 1) Notas de crédito
    for nc in NotaCredito.query.filter(NotaCredito.documento_venta_id.in_(ids)).all():
        if adjust_stock:
            _undo_nota_credito_stock(nc)
        db.session.delete(nc)
    db.session.flush()

    # 2) Facturas/boletas con descuento de stock
    if adjust_stock:
        for d in docs:
            _reverse_stock_for_factura_boleta(d)
        db.session.flush()

    # 3) Garantías: conservar registro, quitar FK al documento
    Garantia.query.filter(Garantia.documento_id.in_(ids)).update(
        {Garantia.documento_id: None}, synchronize_session=False
    )

    # 4) Documentos que quedan (p. ej. orden_compra) no deben apuntar a IDs que borraremos
    otros = DocumentoVenta.query.filter(
        or_(DocumentoVenta.source_id.in_(ids), DocumentoVenta.root_id.in_(ids))
    ).all()
    for o in otros:
        if o.id in ids:
            continue
        if o.source_id in ids:
            o.source_id = None
        if o.root_id in ids:
            o.root_id = None

    # 5) Pickings de OV
    for pv in PickingVenta.query.filter(PickingVenta.orden_venta_id.in_(ids)).all():
        db.session.delete(pv)

    # 6) Documentos de venta (ítems en cascada)
    for d in docs:
        db.session.delete(d)

    db.session.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="Restablece correlativos CO/OV/FA/BO")
    ap.add_argument("--dry-run", action="store_true", help="Solo muestra conteos, no borra nada")
    ap.add_argument("--yes", action="store_true", help="Confirmación explícita para borrar")
    ap.add_argument(
        "--ignore-stock",
        action="store_true",
        help="Eliminar documentos sin ajustar inventario (correlativos en cero; riesgo de stock desalineado)",
    )
    args = ap.parse_args()
    return _run(dry_run=args.dry_run, confirm=args.yes, ignore_stock=args.ignore_stock)


if __name__ == "__main__":
    raise SystemExit(main())
