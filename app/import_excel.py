import pandas as pd
from sqlalchemy import create_engine
import os

# Ruta base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")
EXCEL_PATH = os.path.join(BASE_DIR, "..", "ANDES AUTO PARTS.xlsx")

engine = create_engine(f"sqlite:///{DB_PATH}")

# Leer Excel
df = pd.read_excel(EXCEL_PATH)

# Normalizar nombres
df.columns = [c.strip().upper() for c in df.columns]

# Columnas de stock
stock_cols = [
    "STOCK_10JUL",
    "STOCK_BRASIL",
    "STOCK_G_AVENIDA",
    "STOCK_ORIENTALES",
    "STOCK_B20_OUTLET"
]

for col in stock_cols:
    if col not in df.columns:
        df[col] = 0

# Calcular stock total
df["STOCK_TOTAL"] = df[stock_cols].fillna(0).sum(axis=1)

# Seleccionar y renombrar columnas
productos = df[[
    "CODIGO OEM",
    "CODIGO",
    "DESCRIPCION",
    "MODELO",
    "MOTOR",
    "MARCA",
    "MEDIDAS",
    "HOMOLOGADOS",
    "CODIGO ALTERNATIVO O ANTIGUO",
    "STOCK_TOTAL"
]].copy()

productos.columns = [
    "codigo_oem",
    "codigo_interno",
    "descripcion",
    "modelo",
    "motor",
    "marca",
    "medidas",
    "homologados",
    "codigo_alternativo",
    "stock"
]

# Campos fijos iniciales
productos["bodega"] = "Principal"
productos["costo"] = 0
productos["precio_cliente"] = 0
productos["precio_mayor"] = 0

# Eliminar filas sin códigos válidos
productos = productos.dropna(subset=["codigo_oem", "codigo_interno"])

# Eliminar duplicados por código interno
productos = productos.drop_duplicates(subset=["codigo_interno"])

# Importar a base de datos
productos.to_sql("productos", engine, if_exists="append", index=False)

print("✅ Importación completada con éxito")
print(f"📦 Productos importados: {len(productos)}")
