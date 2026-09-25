from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "data" / "andes.db"


def _load_dataframe(source: Any) -> pd.DataFrame:
	if hasattr(source, "read"):
		filename = getattr(source, "filename", "") or ""
		extension = Path(filename).suffix.lower()
		if hasattr(source, "seek"):
			source.seek(0)
		if extension == ".csv":
			return pd.read_csv(source)
		return pd.read_excel(source)

	path = Path(str(source))
	extension = path.suffix.lower()
	if extension == ".csv":
		return pd.read_csv(path)
	return pd.read_excel(path)


def _normalize_column_name(name: str) -> str:
	return "".join(ch.lower() for ch in str(name or "") if ch.isalnum())


def _normalize_activo_cell(val: Any) -> bool:
	"""True por defecto (catálogo visible). Solo False con valores explícitos de inactivo."""
	if val is None:
		return True
	if isinstance(val, float) and pd.isna(val):
		return True
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)) and not isinstance(val, bool):
		try:
			if val == 0:
				return False
			if val == 1:
				return True
		except Exception:
			pass
		return bool(val)
	s = str(val).strip().lower()
	if not s or s == "nan":
		return True
	if s in ("0", "false", "f", "no", "n", "inactivo", "off"):
		return False
	if s in ("1", "true", "t", "yes", "si", "sí", "activo", "on"):
		return True
	return True


def _normalize_codigo_cell(val: Any) -> str | None:
	"""Unifica códigos para clave única: sin espacios, mayúsculas; números de Excel sin '.0'."""
	if val is None:
		return None
	if isinstance(val, float) and pd.isna(val):
		return None
	if isinstance(val, (int, float)) and not isinstance(val, bool):
		try:
			if float(val) == int(val):
				return str(int(val))
		except (ValueError, OverflowError):
			pass
		return str(val).strip() or None
	s = str(val).strip()
	if not s or s.lower() == "nan":
		return None
	return s.upper()


# Si el Excel ordenó solo algunas columnas, OEM/homologados de un SKU
# aparecen en otro (a menudo de otra marca). Umbrales para abortar.
OEM_SHIFT_MARCA_BLOCK = 12
OEM_SHIFT_TOTAL_BLOCK = 35
HOMOLOG_SHIFT_MARCA_BLOCK = 12
SHIFT_SAMPLE_LIMIT = 12


def _cell_text(val: Any) -> str:
	if val is None:
		return ""
	if isinstance(val, float) and pd.isna(val):
		return ""
	s = str(val).strip()
	if s.lower() == "nan":
		return ""
	return s


def detect_column_shift(
	existing: dict[str, dict[str, Any]],
	incoming: dict[str, dict[str, Any]],
	*,
	oem_marca_block: int = OEM_SHIFT_MARCA_BLOCK,
	oem_total_block: int = OEM_SHIFT_TOTAL_BLOCK,
	homolog_marca_block: int = HOMOLOG_SHIFT_MARCA_BLOCK,
) -> dict[str, Any]:
	"""Compara catálogo actual vs Excel: OEM/homologados que saltaron de un código a otro."""
	oem_owners: dict[str, list[str]] = {}
	hom_owners: dict[str, list[str]] = {}
	for code, row in existing.items():
		oem = _cell_text(row.get("oem")).upper()
		hom = _cell_text(row.get("homologados"))
		if oem:
			oem_owners.setdefault(oem, []).append(code)
		if len(hom) >= 12:
			hom_owners.setdefault(hom.upper(), []).append(code)

	oem_shifts: list[dict[str, str]] = []
	oem_marca_mismatch = 0
	hom_shifts: list[dict[str, str]] = []
	hom_marca_mismatch = 0

	for code, new in incoming.items():
		old = existing.get(code)
		if not old:
			continue
		new_oem = _cell_text(new.get("oem")).upper()
		old_oem = _cell_text(old.get("oem")).upper()
		if new_oem and new_oem != old_oem:
			donors = [c for c in oem_owners.get(new_oem, []) if c != code]
			if donors:
				donor = donors[0]
				donor_row = existing.get(donor) or {}
				marca_sku = _cell_text(old.get("marca")).upper()
				marca_donor = _cell_text(donor_row.get("marca")).upper()
				marca_mismatch = bool(marca_sku and marca_donor and marca_sku != marca_donor)
				if marca_mismatch:
					oem_marca_mismatch += 1
				item = {
					"codigo": code,
					"marca": _cell_text(old.get("marca")) or _cell_text(new.get("marca")),
					"descripcion": _cell_text(old.get("descripcion")) or _cell_text(new.get("descripcion")),
					"oem_excel": _cell_text(new.get("oem")),
					"oem_actual": _cell_text(old.get("oem")),
					"pertenecia_a": donor,
					"marca_origen": _cell_text(donor_row.get("marca")),
				}
				oem_shifts.append(item)

		new_hom = _cell_text(new.get("homologados"))
		old_hom = _cell_text(old.get("homologados"))
		if len(new_hom) >= 12 and new_hom.upper() != old_hom.upper():
			donors = [c for c in hom_owners.get(new_hom.upper(), []) if c != code]
			if donors:
				donor = donors[0]
				donor_row = existing.get(donor) or {}
				marca_sku = _cell_text(old.get("marca")).upper()
				marca_donor = _cell_text(donor_row.get("marca")).upper()
				marca_mismatch = bool(marca_sku and marca_donor and marca_sku != marca_donor)
				if marca_mismatch:
					hom_marca_mismatch += 1
				hom_shifts.append(
					{
						"codigo": code,
						"marca": _cell_text(old.get("marca")) or _cell_text(new.get("marca")),
						"homologados_excel": new_hom[:120],
						"pertenecia_a": donor,
						"marca_origen": _cell_text(donor_row.get("marca")),
					}
				)

	blocked = (
		oem_marca_mismatch >= oem_marca_block
		or len(oem_shifts) >= oem_total_block
		or hom_marca_mismatch >= homolog_marca_block
	)
	reason = ""
	if blocked:
		reason = (
			"El Excel parece tener columnas corridas (OEM u homologados de un producto "
			"en la fila de otro, muchas veces de otra marca). No se importó nada. "
			"Revisa que al ordenar o pegar en Excel estén seleccionadas TODAS las columnas."
		)
	return {
		"blocked": blocked,
		"reason": reason,
		"oem_shifts": len(oem_shifts),
		"oem_marca_mismatch": oem_marca_mismatch,
		"homolog_shifts": len(hom_shifts),
		"homolog_marca_mismatch": hom_marca_mismatch,
		"samples": oem_shifts[:SHIFT_SAMPLE_LIMIT],
		"homolog_samples": hom_shifts[:SHIFT_SAMPLE_LIMIT],
	}


def _incoming_from_dataframe(dataframe: pd.DataFrame) -> dict[str, dict[str, Any]]:
	out: dict[str, dict[str, Any]] = {}
	if "CODIGO" not in dataframe.columns:
		return out
	oem_col = "CODIGO OEM" if "CODIGO OEM" in dataframe.columns else None
	hom_col = "HOMOLOGADOS" if "HOMOLOGADOS" in dataframe.columns else None
	marca_col = "MARCA" if "MARCA" in dataframe.columns else None
	desc_col = "DESCRIPCION" if "DESCRIPCION" in dataframe.columns else None
	for rec in dataframe.to_dict("records"):
		code = _normalize_codigo_cell(rec.get("CODIGO"))
		if not code:
			continue
		out[code] = {
			"oem": rec.get(oem_col) if oem_col else None,
			"homologados": rec.get(hom_col) if hom_col else None,
			"marca": rec.get(marca_col) if marca_col else None,
			"descripcion": rec.get(desc_col) if desc_col else None,
		}
	return out


def _overlay_missing_columns(
	dataframe: pd.DataFrame,
	table_columns: list[str],
	excel_db_cols: set[str],
	existing_rows: dict[str, dict[str, Any]],
) -> pd.DataFrame:
	"""Si el Excel no trae una columna (precio, stock, imagen), conserva el valor actual."""
	if not existing_rows or not table_columns:
		return dataframe
	records = dataframe.to_dict("records")
	for rec in records:
		code = rec.get("CODIGO")
		old = existing_rows.get(code) if code else None
		if not old:
			continue
		for col in table_columns:
			if col not in excel_db_cols:
				rec[col] = old.get(col)
	return pd.DataFrame.from_records(records, columns=table_columns)


def import_products_from_excel(
	source: Any,
	batch_size: int = 2000,
	*,
	ignore_shift_guard: bool = False,
) -> dict[str, Any]:
	start_time = time.perf_counter()

	if source is None:
		raise ValueError("No se proporcionó archivo para importar")

	dataframe = _load_dataframe(source)
	if dataframe.empty:
		return {
			"status": "ok",
			"inserted": 0,
			"updated": 0,
			"skipped": 0,
			"import_notes": [],
			"errors": [],
			"errors_count": 0,
			"time_seconds": round(time.perf_counter() - start_time, 3),
			"batch_size": batch_size,
		}

	dataframe = dataframe.where(pd.notnull(dataframe), None)

	engine = create_engine(f"sqlite:///{DB_PATH}")
	with engine.begin() as connection:
		existing_count = connection.execute(text("SELECT COUNT(*) FROM productos")).scalar() or 0
		schema_rows = connection.exec_driver_sql("PRAGMA table_info(productos)").fetchall()
		table_columns = [row[1] for row in schema_rows]
		excel_db_cols: set[str] = set()

		if table_columns:
			source_columns = {_normalize_column_name(column): column for column in dataframe.columns}
			aligned_rows = {}
			for column in table_columns:
				matched = source_columns.get(_normalize_column_name(column))
				if matched is None:
					aligned_rows[column] = None
				else:
					excel_db_cols.add(column)
					aligned_rows[column] = dataframe[matched]
			dataframe = pd.DataFrame(aligned_rows)

		rows_before = len(dataframe)
		empty_codigo_rows = 0
		dedup_removed = 0
		codigo_col = "CODIGO"
		if codigo_col in dataframe.columns:
			normalized = dataframe[codigo_col].map(_normalize_codigo_cell)
			empty_codigo_rows = int(normalized.isna().sum())
			dataframe = dataframe.assign(**{codigo_col: normalized})
			dataframe = dataframe[dataframe[codigo_col].notna()].copy()
			rows_nonempty = len(dataframe)
			# Última fila gana (mismo CODIGO repetido en el Excel → UNIQUE en SQLite)
			dataframe = dataframe.drop_duplicates(subset=[codigo_col], keep="last")
			dedup_removed = max(0, rows_nonempty - len(dataframe))

		existing_full: dict[str, dict[str, Any]] = {}
		if table_columns and existing_count:
			quoted = ", ".join(f'"{c}"' for c in table_columns)
			for row in connection.execute(text(f"SELECT {quoted} FROM productos")).mappings():
				code = _normalize_codigo_cell(row.get("CODIGO"))
				if code:
					existing_full[code] = dict(row)

		if existing_full and not ignore_shift_guard:
			incoming = _incoming_from_dataframe(dataframe)
			existing_snap = {
				code: {
					"descripcion": row.get("DESCRIPCION"),
					"marca": row.get("MARCA"),
					"oem": row.get("CODIGO OEM"),
					"homologados": row.get("HOMOLOGADOS"),
				}
				for code, row in existing_full.items()
			}
			shift_report = detect_column_shift(existing_snap, incoming)
			if shift_report.get("blocked"):
				return {
					"status": "blocked",
					"inserted": 0,
					"updated": 0,
					"skipped": rows_before,
					"empty_codigo_skipped": empty_codigo_rows,
					"dedup_removed": dedup_removed,
					"import_notes": [shift_report.get("reason") or ""],
					"errors": [shift_report.get("reason") or "Columnas corridas"],
					"errors_count": 1,
					"time_seconds": round(time.perf_counter() - start_time, 3),
					"batch_size": batch_size,
					"shift_report": shift_report,
				}

		if existing_full and table_columns:
			dataframe = _overlay_missing_columns(
				dataframe, table_columns, excel_db_cols, existing_full
			)

		act_col = "ACTIVO"
		if act_col in excel_db_cols:
			dataframe = dataframe.assign(
				**{act_col: dataframe[act_col].map(_normalize_activo_cell)}
			)
		elif act_col in dataframe.columns:
			dataframe = dataframe.assign(
				**{
					act_col: dataframe[act_col].map(
						lambda v: True
						if v is None or (isinstance(v, float) and pd.isna(v))
						else v
					)
				}
			)
		else:
			dataframe = dataframe.assign(**{act_col: True})

		connection.execute(text("DELETE FROM productos"))
		dataframe.to_sql("productos", connection, if_exists="append", index=False)
		# Seguridad: Excel sin columna ACTIVO o celdas vacías → NULL en SQLite y no aparecen en búsqueda
		connection.execute(
			text("UPDATE productos SET ACTIVO = 1 WHERE ACTIVO IS NULL")
		)

	try:
		from app.utils.fts_productos import fts_create_table, fts_rebuild

		with engine.begin() as conn:
			fts_create_table(conn)
			fts_rebuild(conn)
	except Exception:
		pass

	inserted = int(len(dataframe))
	updated = int(min(existing_count, inserted))
	skipped = max(0, rows_before - len(dataframe))
	import_notes: list[str] = []
	if empty_codigo_rows:
		import_notes.append(f"{empty_codigo_rows} fila(s) sin CODIGO válido omitidas")
	if dedup_removed:
		import_notes.append(
			f"{dedup_removed} fila(s) duplicadas por CODIGO en el Excel (se mantuvo la última)"
		)
	if table_columns and excel_db_cols:
		preserved = [c for c in table_columns if c not in excel_db_cols]
		if preserved:
			import_notes.append(
				"Columnas que no venían en el Excel se conservaron del catálogo "
				"(precios, stock, imágenes, categorías, etc.)."
			)

	try:
		from app.models import SessionDB
		from app.utils.categoria_autodetect import bulk_auto_asignar_categorias_faltantes

		_sess = SessionDB()
		try:
			_n = bulk_auto_asignar_categorias_faltantes(_sess)
			if _n:
				import_notes.append(
					f"Categoría/subcategoría autoasignadas en {_n} producto(s) según descripción y OEM"
				)
		finally:
			_sess.close()
	except Exception as exc:
		import_notes.append(f"Aviso: autoasignación de categorías omitida ({exc})")

	return {
		"status": "ok",
		"inserted": inserted,
		"updated": updated,
		"skipped": skipped,
		"empty_codigo_skipped": empty_codigo_rows,
		"dedup_removed": dedup_removed,
		"import_notes": import_notes,
		"errors": [],
		"errors_count": 0,
		"time_seconds": round(time.perf_counter() - start_time, 3),
		"batch_size": batch_size,
	}