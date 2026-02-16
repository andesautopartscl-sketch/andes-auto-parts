from app import SessionDB, Usuario

db = SessionDB()

admin = Usuario(
    username="albertadmin",
    password="1234",
    rol="admin"
)

db.add(admin)
db.commit()
db.close()

print("Usuario creado correctamente")