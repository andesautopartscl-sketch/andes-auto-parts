from flask import Flask, request, send_file, redirect, url_for, session, jsonify
from sqlalchemy import create_engine, Column, String, Integer, Float, or_
from sqlalchemy.orm import declarative_base, sessionmaker
import os
import pandas as pd
import io
import re
import socket
from flask import render_template


# ======================================================
# APP
# ======================================================

app = Flask(__name__)
app.secret_key = "andes_auto_parts_secret"


# ======================================================
# DATABASE
# ======================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "andes.db")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

# ======================================================
# MODELOS
# ======================================================

class Usuario(Base):
    __tablename__ = "usuarios"

    username = Column(String, primary_key=True)
    password = Column(String)
    rol = Column(String)


class Producto(Base):
    __tablename__ = "productos"

    codigo = Column("CODIGO", String, primary_key=True)

    descripcion = Column("DESCRIPCION", String)
    modelo = Column("MODELO", String)
    motor = Column("MOTOR", String)
    marca = Column("MARCA", String)

    p_publico = Column("P_PUBLICO", Float)
    p_pub_dsc = Column("P_PUB_DSC", Float)

    stock_10jul = Column("STOCK_10JUL", Float)
    stock_brasil = Column("STOCK_BRASIL", Float)
    stock_g_avenida = Column("STOCK_G_AVENIDA", Float)
    stock_orientales = Column("STOCK_ORIENTALES", Float)
    stock_b20_outlet = Column("STOCK_B20_OUTLET", Float)
    stock_transito = Column("STOCK_TRANSITO", Float)

    precio_pagar_plaza = Column("PRECIO A PAGAR PLAZA", Float)
    precio_neto_plaza = Column("PRECIO NETO PLAZA", Float)

    pedido = Column("PEDIDO", Float)

    prec_mayor = Column("PREC_MAYOR", Float)
    p_mayor_dsc = Column("P_MAYOR_DSC", Float)

    medidas = Column("MEDIDAS", String)
    codigo_oem = Column("CODIGO OEM", String)
    codigo_alternativo = Column("CODIGO ALTERNATIVO O ANTIGUO", String)
    homologados = Column("HOMOLOGADOS", String)

# ======================================================
# HELPERS
# ======================================================
def login_required():
    return "user" in session

def resaltar(txt, palabras):
    if not txt:
        return ""
    for p in palabras:
        txt = re.sub(
            f"({re.escape(p)})",
            r"<span style='background:yellow'>\1</span>",
            txt,
            flags=re.I
        )
    return txt

def score_producto(p):
    score = 0

    if not palabras:
        return stock_total(p)

    texto = (
        f"{safe(p.codigo)} {safe(p.descripcion)} "
        f"{safe(p.modelo)} {safe(p.marca)} "
        f"{safe(p.codigo_oem)} {safe(p.codigo_alternativo)} "
        f"{safe(p.homologados)}"
    ).lower()

    for palabra in palabras:

        # PRIORIDAD MAXIMA → CODIGO
        if palabra in safe(p.codigo).lower():
            score += 150

        # SEGUNDA PRIORIDAD → MODELO
        if palabra in safe(p.modelo).lower():
            score += 120

        # TERCERA → OEM
        if palabra in safe(p.codigo_oem).lower():
            score += 100

        # CUARTA → DESCRIPCION
        if palabra in safe(p.descripcion).lower():
            score += 70

        # RESTO DEL TEXTO
        if palabra in texto:
            score += 25

    # Bonus por stock
    score += stock_total(p) * 2

    return score
# ======================================================
# LOGIN
# ======================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":

        username = request.form.get("username")
        password = request.form.get("password")

        db = SessionDB()

        user = db.query(Usuario).filter_by(username=username).first()

        db.close()

        if user and user.password == password:

            session["user"] = user.username
            session["rol"] = user.rol

            return redirect(url_for("buscar"))

        else:
            error = "Usuario o clave incorrectos"

    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def home():
    return redirect(url_for("buscar"))

# ======================================================
# BUSCAR (BARRA + FILTROS RESTAURADOS)
# ======================================================
@app.route("/buscar")
def buscar():
    if not login_required():
        return redirect(url_for("login"))

    q = request.args.get("q", "").strip()
    f_codigo = request.args.get("f_codigo", "").strip()
    f_desc = request.args.get("f_desc", "").strip()
    f_modelo = request.args.get("f_modelo", "").strip()
    f_marca = request.args.get("f_marca", "").strip()
    f_oem = request.args.get("f_oem", "").strip()

    palabras = q.lower().split()

    db = SessionDB()
    query = db.query(Producto)

    if palabras:
        for palabra in palabras:
            query = query.filter(
                or_(
                    Producto.codigo.ilike(f"%{palabra}%"),
                    Producto.descripcion.ilike(f"%{palabra}%"),
                    Producto.modelo.ilike(f"%{palabra}%"),
                    Producto.motor.ilike(f"%{palabra}%"),
                    Producto.marca.ilike(f"%{palabra}%"),
                    Producto.codigo_oem.ilike(f"%{palabra}%"),
                    Producto.codigo_alternativo.ilike(f"%{palabra}%"),
                    Producto.homologados.ilike(f"%{palabra}%")
                )
            )

    if f_codigo:
        query = query.filter(Producto.codigo.ilike(f"%{f_codigo}%"))
    if f_desc:
        query = query.filter(Producto.descripcion.ilike(f"%{f_desc}%"))
    if f_modelo:
        query = query.filter(Producto.modelo.ilike(f"%{f_modelo}%"))
    if f_marca:
        query = query.filter(Producto.marca.ilike(f"%{f_marca}%"))
    if f_oem:
        query = query.filter(Producto.codigo_oem.ilike(f"%{f_oem}%"))

    productos = query.limit(3000).all()
    db.close()

    def safe(val):
        return (val or "").strip()

    def stock_total(p):
        return (
            (p.stock_10jul or 0) +
            (p.stock_brasil or 0) +
            (p.stock_g_avenida or 0) +
            (p.stock_orientales or 0) +
            (p.stock_b20_outlet or 0) +
            (p.stock_transito or 0)
        )

    productos.sort(key=lambda p: safe(p.descripcion).lower())

    return render_template(
    "buscar.html",
    productos=productos,
    q=q,
    session=session,
    stock_total=stock_total
)
    
# ======================================================
# EXPORTAR
# ======================================================
@app.route("/exportar")
def exportar():
    if not login_required() or session["rol"]!="admin":
        return "No autorizado"

    db = SessionDB()
    productos = db.query(Producto).all()
    db.close()

    df = pd.DataFrame([p.__dict__ for p in productos])
    df.drop(columns=["_sa_instance_state"], inplace=True)

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="andes_autoparts.xlsx")

from flask import jsonify

@app.route("/importar_excel", methods=["POST"])
def importar_excel():

    if not login_required():
        return jsonify(success=False, message="No autorizado")

    if session["rol"] != "admin":
        return jsonify(success=False, message="Acceso denegado")

    archivo = request.files.get("archivo")

    if not archivo:
        return jsonify(success=False, message="No se seleccionó archivo")

    try:
        df = pd.read_excel(archivo)

        db = SessionDB()

        # 🔥 BORRAR TODO
        db.query(Producto).delete()
        db.commit()

        contador = 0

        for _, row in df.iterrows():
            producto = Producto(
                codigo=str(row.get("CODIGO", "")).strip(),
                descripcion=str(row.get("DESCRIPCION", "")).strip(),
                modelo=str(row.get("MODELO", "")).strip(),
                motor=str(row.get("MOTOR", "")).strip(),
                marca=str(row.get("MARCA", "")).strip(),
                p_publico=row.get("P_PUBLICO", 0) or 0,
                prec_mayor=row.get("PREC_MAYOR", 0) or 0,
                codigo_oem=str(row.get("CODIGO OEM", "")).strip(),
                codigo_alternativo=str(row.get("CODIGO ALTERNATIVO O ANTIGUO", "")).strip(),
                homologados=str(row.get("HOMOLOGADOS", "")).strip(),
                stock_10jul=row.get("STOCK_10JUL", 0) or 0,
                stock_brasil=row.get("STOCK_BRASIL", 0) or 0,
                stock_g_avenida=row.get("STOCK_G_AVENIDA", 0) or 0,
                stock_orientales=row.get("STOCK_ORIENTALES", 0) or 0,
                stock_b20_outlet=row.get("STOCK_B20_OUTLET", 0) or 0,
                stock_transito=row.get("STOCK_TRANSITO", 0) or 0
            )

            db.add(producto)
            contador += 1

        db.commit()
        db.close()

        return jsonify(success=True, message=f"{contador} productos cargados correctamente")

    except Exception as e:
        return jsonify(success=False, message=str(e))

# ======================================================
# MAIN
# ======================================================
if __name__ == "__main__":
    hostname = socket.gethostname()
    print(f"🌐 http://{socket.gethostbyname(hostname)}:5000/login")
    app.run(host="0.0.0.0", port=5000, debug=True)
