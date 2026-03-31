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


def import_products_from_excel(source: Any, batch_size: int = 2000) -> dict[str, Any]:
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

		if table_columns:
			source_columns = {_normalize_column_name(column): column for column in dataframe.columns}
			aligned_rows = {}
			for column in table_columns:
				matched = source_columns.get(_normalize_column_name(column))
				if matched is None:
					aligned_rows[column] = None
				else:
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

		act_col = "ACTIVO"
		if act_col in dataframe.columns:
			dataframe = dataframe.assign(
				**{act_col: dataframe[act_col].map(_normalize_activo_cell)}
			)
		else:
			dataframe = dataframe.assign(**{act_col: True})

		connection.execute(text("DELETE FROM productos"))
		dataframe.to_sql("productos", connection, if_exists="append", index=False)
		# Seguridad: Excel sin columna ACTIVO o celdas vacías → NULL en SQLite y no aparecen en búsqueda
		connection.execute(
			text("UPDATE productos SET ACTIVO = 1 WHERE ACTIVO IS NULL")
		)

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