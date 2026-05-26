from app import create_app
from app.seguridad.models import Usuario

app = create_app()

with app.app_context():
    print("Testing Usuario.query.all():")
    users = Usuario.query.all()
    print(f"  Found {len(users)} users")
    for u in users:
        print(f"    - {u.usuario} ({u.nombre})")
        print(f"      ID: {u.id}, Active: {u.activo}")
