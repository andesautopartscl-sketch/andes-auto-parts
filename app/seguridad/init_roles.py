from app.extensions import db
from app.seguridad.models import Rol

def crear_roles():

    roles = [
        ("SuperAdmin", 100),
        ("Dueño", 90),
        ("Gerente", 80),
        ("SubGerente", 70),
        ("Encargado", 60),
        ("Vendedor", 50),
        ("Bodeguero", 40),
        ("Transportista", 30)
    ]

    for nombre, nivel in roles:

        existe = Rol.query.filter_by(nombre=nombre).first()

        if not existe:

            rol = Rol(
                nombre=nombre,
                nivel=nivel,
                descripcion=f"Rol {nombre}"
            )

            db.session.add(rol)

    db.session.commit()

    print("Roles iniciales creados")