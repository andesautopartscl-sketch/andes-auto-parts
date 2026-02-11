from flask import Flask, request, send_file, redirect, url_for, session
from sqlalchemy import create_engine, Column, String, Integer, Float, or_
from sqlalchemy.orm import declarative_base, sessionmaker
import os
import pandas as pd
import io
import re
import socket

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
    id = Column(Integer, primary_key=True, autoincrement=True)
    codigo_interno = Column(String)
    descripcion = Column(String)
    modelo = Column(String)
    motor = Column(String)
    marca = Column(String)
    costo = Column(Float, default=0)
    precio_cliente = Column(Float, default=0)
    precio_mayor = Column(Float, default=0)
    stock = Column(Integer, default=0)
    medidas = Column(String)
    codigo_oem = Column(String)
    codigo_alternativo = Column(String)
    homologados = Column(String)

Base.metadata.create_all(engine)

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

def score_producto(p, palabras):
    score = 0
    for w in palabras:
        w = w.lower()
        if p.modelo and w in p.modelo.lower(): score += 5
        if p.descripcion and w in p.descripcion.lower(): score += 4
        if p.marca and w in p.marca.lower(): score += 3
        if p.motor and w in p.motor.lower(): score += 2
        if p.codigo_oem and w in p.codigo_oem.lower(): score += 2
        if p.codigo_alternativo and w in p.codigo_alternativo.lower(): score += 1
        if p.homologados and w in p.homologados.lower(): score += 1
    return score

# ======================================================
# LOGIN (NO SE TOCA)
# ======================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        db = SessionDB()
        u = db.query(Usuario).filter_by(
            username=request.form["username"],
            password=request.form["password"]
        ).first()
        db.close()

        if u:
            session["user"] = u.username
            session["rol"] = u.rol
            return redirect(url_for("buscar"))
        error = "Usuario o clave incorrectos"

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Login | Andes Auto Parts</title>

<script src="https://cdn.jsdelivr.net/npm/particles.js@2.0.0/particles.min.js"></script>

<style>
html, body {{
    margin:0;
    padding:0;
    height:100%;
    font-family: Arial;
    background:#0f1e46;
    overflow:hidden;
}}

#particles-js {{
    position:fixed;
    width:100%;
    height:100%;
    z-index:1;
}}

.login-wrapper {{
    height:100%;
    display:flex;
    align-items:center;
    justify-content:center;
    position:relative;
    z-index:2;
}}

.login-box {{
    width:360px;
    background:#162a63;
    padding:30px 25px;
    border-radius:14px;
    box-shadow:0 20px 40px rgba(0,0,0,.45);
    text-align:center;
}}

.login-box img {{
    width:120px;
    margin-bottom:15px;
}}

.login-box h2 {{
    color:white;
    margin-bottom:20px;
}}

.login-box input {{
    width:85%;
    padding:11px;
    margin-bottom:12px;
    border:none;
    border-radius:6px;
    outline:none;
    text-align:left;
}}

.login-box button {{
    width:75%;
    padding:10px;
    background:#2f6bff;
    border:none;
    border-radius:6px;
    color:white;
    font-weight:bold;
    cursor:pointer;
    margin-top:5px;
}}

.login-box button:hover {{
    background:#1f4fd8;
}}

.error {{
    color:#ff8080;
    margin-top:10px;
    font-size:13px;
}}
</style>
</head>

<body>

<div id="particles-js"></div>

<div class="login-wrapper">
    <div class="login-box">
        <img src="/static/logo.png" alt="Andes Auto Parts">
        <h2>Andes Auto Parts</h2>
        <form method="post">
            <input name="username" placeholder="Usuario" required>
            <input type="password" name="password" placeholder="Clave" required>
            <button type="submit">Ingresar</button>
        </form>
        <div class="error">{error}</div>
    </div>
</div>

<script>
particlesJS("particles-js", {{
  particles: {{
    number: {{ value: 60 }},
    color: {{ value: "#ffffff" }},
    shape: {{ type: "circle" }},
    opacity: {{ value: 0.5 }},
    size: {{ value: 3 }},
    line_linked: {{
      enable: true,
      distance: 150,
      color: "#ffffff",
      opacity: 0.3,
      width: 1
    }},
    move: {{
      enable: true,
      speed: 2
    }}
  }},
  interactivity: {{
    detect_on: "canvas",
    events: {{
      onhover: {{
        enable: false
      }},
      onclick: {{
        enable: true,
        mode: "push"
      }}
    }},
    modes: {{
      push: {{
        particles_nb: 8
      }}
    }}
  }},
  retina_detect: true
}});
</script>



</body>
</html>
"""

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

    q = request.args.get("q", "")
    f_codigo = request.args.get("f_codigo", "")
    f_desc = request.args.get("f_desc", "")
    f_modelo = request.args.get("f_modelo", "")
    f_marca = request.args.get("f_marca", "")
    f_oem = request.args.get("f_oem", "")

    palabras = q.lower().split()

    db = SessionDB()
    query = db.query(Producto)

    if q:
        for p in palabras:
            query = query.filter(or_(
                Producto.codigo_interno.ilike(f"%{p}%"),
                Producto.descripcion.ilike(f"%{p}%"),
                Producto.modelo.ilike(f"%{p}%"),
                Producto.motor.ilike(f"%{p}%"),
                Producto.marca.ilike(f"%{p}%"),
                Producto.codigo_oem.ilike(f"%{p}%"),
                Producto.codigo_alternativo.ilike(f"%{p}%"),
                Producto.homologados.ilike(f"%{p}%")
            ))

    if f_codigo: query = query.filter(Producto.codigo_interno.ilike(f"%{f_codigo}%"))
    if f_desc: query = query.filter(Producto.descripcion.ilike(f"%{f_desc}%"))
    if f_modelo: query = query.filter(Producto.modelo.ilike(f"%{f_modelo}%"))
    if f_marca: query = query.filter(Producto.marca.ilike(f"%{f_marca}%"))
    if f_oem: query = query.filter(Producto.codigo_oem.ilike(f"%{f_oem}%"))

    productos = query.limit(300).all()
    db.close()

    if palabras:
        productos.sort(key=lambda p: score_producto(p, palabras), reverse=True)

    export_btn = "<a href='/exportar'>📥 Exportar Excel</a>" if session["rol"]=="admin" else ""

    html = f"""
    <style>
    body{{margin:0;font-family:Arial}}
    .layout{{display:flex;height:100vh}}
    .sidebar{{width:220px;background:#1f4fd8;color:white;padding:20px}}
    .sidebar a{{display:block;color:white;margin:10px 0;font-weight:bold;text-decoration:none}}
    .content{{flex:1;padding:20px;overflow:auto}}
    table{{width:100%;border-collapse:collapse;background:white}}
    th{{background:#0d2fa4;color:white;padding:6px;position:sticky;top:0}}
    td {{padding:4px 6px;border-bottom:1px solid #ddd;vertical-align:top;font-size:11px;line-height:1.1;}}
    td td.small {{font-size:12px;line-height:1.15;}}
    .filters input{{width:100%;font-size:12px}}
    </style>

    <div class="layout">
    <div class="sidebar">
        <h3>Andes Auto Parts</h3>
        <a href="/buscar">🔍 Buscar</a>
        <a href="#">➕ Crear</a>
        <a href="#">✏ Editar</a>
        <a href="#">🗑 Eliminar</a>
        {export_btn}
        <hr>
        <a href="/logout">🚪 Salir</a>
    </div>

    <div class="content">
        <h2>Buscar productos</h2>
        <p>Usuario: {session['user']} ({session['rol']})</p>

        <form method="get">
            <input name="q" value="{q}" placeholder="🔎 Búsqueda general">
            <button>Buscar</button>
        </form>

        <p><b>Resultados:</b> {len(productos)}</p>

        <table>
        <tr class="filters">
            <th><input name="f_codigo" value="{f_codigo}" placeholder="Código"></th>
            <th><input name="f_desc" value="{f_desc}" placeholder="Descripción"></th>
            <th><input name="f_modelo" value="{f_modelo}" placeholder="Modelo"></th>
            <th></th>
            <th><input name="f_marca" value="{f_marca}" placeholder="Marca"></th>
            <th></th><th></th><th></th>
            <th><input name="f_oem" value="{f_oem}" placeholder="OEM"></th>
            <th></th><th></th>
        </tr>

        <tr>
            <th>Código</th><th>Descripción</th><th>Modelo</th><th>Motor</th>
            <th>Marca</th><th>Precio</th><th>Mayor</th><th>Stock</th>
            <th>OEM</th><th>Alternativo</th><th>Homologados</th>
        </tr>
    """

    for p in productos:
        html += f"""
        <tr>
            <td>{resaltar(p.codigo_interno,palabras)}</td>
            <td>{resaltar(p.descripcion,palabras)}</td>
            <td>{resaltar(p.modelo,palabras)}</td>
            <td>{p.motor}</td>
            <td>{p.marca}</td>
            <td>${p.precio_cliente}</td>
            <td>${p.precio_mayor}</td>
            <td>{p.stock}</td>
            <td>{p.codigo_oem}</td>
            <td>{p.codigo_alternativo}</td>
            <td>{p.homologados}</td>
        </tr>
        """

    html += "</table></div></div>"
    return html

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

# ======================================================
# MAIN
# ======================================================
if __name__ == "__main__":
    hostname = socket.gethostname()
    print(f"🌐 http://{socket.gethostbyname(hostname)}:5000/login")
    app.run(host="0.0.0.0", port=5000, debug=True)
