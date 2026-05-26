#!/usr/bin/env python
from app import create_app
from app.extensions import db
from app.inventario.models import LabelPrintHistory

app = create_app()
app.app_context().push()

# Check if table exists
inspector = db.inspect(db.engine)
tables = inspector.get_table_names()
print("TABLE EXISTS:", "inventario_label_print_history" in tables)

if "inventario_label_print_history" in tables:
    count = db.session.query(LabelPrintHistory).count()
    print(f"RECORDS IN TABLE: {count}")
else:
    print("Creating table...")
    LabelPrintHistory.__table__.create(db.engine, checkfirst=True)
    print("TABLE CREATED")
