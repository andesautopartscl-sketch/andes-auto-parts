#!/usr/bin/env python3
"""Ensure albertadmin can authenticate using the app's current hashing method."""

from app import create_app
from app.extensions import db
from app.seguridad.models import Usuario, Rol
from werkzeug.security import generate_password_hash, check_password_hash

TARGET_USER = "albertadmin"
TARGET_PASSWORD = "1234"
TARGET_ROLE_NAME = "SuperAdmin"


def main() -> None:
    app = create_app()

    with app.app_context():
        role = Rol.query.filter_by(nombre=TARGET_ROLE_NAME).first()
        if role is None:
            role = Rol(nombre=TARGET_ROLE_NAME, nivel=100, descripcion="SuperAdmin")
            db.session.add(role)
            db.session.commit()
            print("[FIX] Created missing role: SuperAdmin")

        user = Usuario.query.filter_by(usuario=TARGET_USER).first()
        new_hash = generate_password_hash(TARGET_PASSWORD)

        if user is None:
            user = Usuario(
                nombre="Albert Admin",
                usuario=TARGET_USER,
                password_hash=new_hash,
                rol_id=role.id,
                activo=True,
            )
            db.session.add(user)
            action = "created"
        else:
            user.password_hash = new_hash
            user.rol_id = role.id
            user.activo = True
            action = "updated"

        db.session.commit()

        verified = check_password_hash(user.password_hash, TARGET_PASSWORD)
        print(f"[FIX] User {action}: {TARGET_USER}")
        print(f"[FIX] Role: {user.rol.nombre if user.rol else 'None'}")
        print(f"[FIX] Active: {user.activo}")
        print(f"[FIX] Password hash type: {'scrypt' if user.password_hash.startswith('scrypt:') else 'pbkdf2/other'}")
        print(f"[FIX] Password verification (1234): {verified}")


if __name__ == "__main__":
    main()
