from models import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text(
        "ALTER TABLE productos_comerciales ADD COLUMN activo BOOLEAN DEFAULT 1"
    ))
    conn.commit()

print("Columna 'activo' agregada correctamente")