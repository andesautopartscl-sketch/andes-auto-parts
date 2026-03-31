from app.extensions import db
from app.seguridad.models import Usuario, Rol
from werkzeug.security import generate_password_hash

def crear_superadmin():

    rol = Rol.query.filter_by(nombre="SuperAdmin").first()

    if not rol:
        print("Rol SuperAdmin no encontrado")
        return

    existe = Usuario.query.filter_by(usuario="albert").first()

    if existe:
        print("SuperAdmin ya existe")
        return

    nuevo = Usuario(
        nombre="Albert Castillo",
        usuario="albert",
        password_hash=generate_password_hash("123456"),
        rol_id=rol.id,
        activo=True
    )

    db.session.add(nuevo)
    db.session.commit()

    print("SuperAdmin creado correctamente")