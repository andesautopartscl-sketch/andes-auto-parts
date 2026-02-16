import pandas as pd
from sqlalchemy import create_engine
import os

# ==============================
# RUTAS
# ==============================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")
EXCEL_PATH = os.path.join(BASE_DIR, "..", "data", "productos.xlsx")

print("Base usada:", DB_PATH)
print("Excel usado:", EXCEL_PATH)

# ==============================
# CONEXIÓN BASE
# ==============================

engine = create_engine(f"sqlite:///{DB_PATH}")

# ==============================
# LEER EXCEL
# ==============================

df = pd.read_excel(EXCEL_PATH)

print("Filas en Excel:", len(df))

# ==============================
# GUARDAR EN SQLITE
# ==============================

df.to_sql("productos", engine, if_exists="replace", index=False)

print("✅ Importación completada correctamente")