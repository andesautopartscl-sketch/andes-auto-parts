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

    q = request.args.get("q", "").strip()
    f_codigo = request.args.get("f_codigo", "").strip()
    f_desc = request.args.get("f_desc", "").strip()
    f_modelo = request.args.get("f_modelo", "").strip()
    f_marca = request.args.get("f_marca", "").strip()
    f_oem = request.args.get("f_oem", "").strip()

    palabras = q.lower().split()
    texto_completo = q.lower()

    db = SessionDB()
    query = db.query(Producto)

    # =========================
    # BUSQUEDA GENERAL
    # =========================
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

    # =========================
    # FILTROS INDIVIDUALES
    # =========================
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

    # =========================
    # FUNCIONES AUXILIARES
    # =========================
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

    # =========================
    # ORDENAMIENTO ULTRA PRO
    # =========================
    if palabras:

        def texto_total_producto(p):
            return f"{safe(p.descripcion)} {safe(p.modelo)} {safe(p.marca)}".lower()

        def match_grupo_completo(p):
            texto = texto_total_producto(p)
            return all(palabra in texto for palabra in palabras)

        def match_descripcion(p):
            descripcion = safe(p.descripcion).lower()
            return all(palabra in descripcion for palabra in palabras)

        def match_modelo(p):
            modelo = safe(p.modelo).lower()
            return all(palabra in modelo for palabra in palabras)

        productos.sort(
            key=lambda p: (
                0 if match_grupo_completo(p) else 1,  # 🔥 PRIORIDAD MÁXIMA (sensor + jac + x200)
                0 if match_descripcion(p) else 1,
                0 if match_modelo(p) else 1,
                safe(p.descripcion) == "",
                safe(p.descripcion).lower()
            )
        )

    else:
        productos.sort(
            key=lambda p: (
                safe(p.descripcion) == "",
                safe(p.descripcion).lower()
            )
        )

    export_btn = "<a href='/exportar'>📥 Exportar Excel</a>" if session["rol"] == "admin" else ""

    # =========================
    # HTML
    # =========================
    html = f"""
    <style>
    body{{margin:0;font-family:Arial}}
    .layout{{display:flex;height:100vh}}
    .sidebar{{width:220px;background:#1f4fd8;color:white;padding:20px}}
    .sidebar a{{display:block;color:white;margin:10px 0;font-weight:bold;text-decoration:none}}
    .content{{flex:1;padding:20px;overflow:auto}}
    table{{width:100%;border-collapse:collapse;background:white}}
    th{{background:#0d2fa4;color:white;padding:6px;position:sticky;top:0}}
    td{{padding:4px 6px;border-bottom:1px solid #ddd;font-size:11px}}
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
        <tr>
            <th>Código</th>
            <th>Descripción</th>
            <th>Modelo</th>
            <th>Motor</th>
            <th>Marca</th>
            <th>Precio</th>
            <th>Mayor</th>
            <th>Stock</th>
            <th>OEM</th>
            <th>Alternativo</th>
            <th>Homologados</th>
        </tr>
    """

    for p in productos:
        html += f"""
        <tr>
            <td>{safe(p.codigo)}</td>
            <td>{safe(p.descripcion)}</td>
            <td>{safe(p.modelo)}</td>
            <td>{safe(p.motor)}</td>
            <td>{safe(p.marca)}</td>
            <td>${p.p_publico or 0}</td>
            <td>${p.prec_mayor or 0}</td>
            <td>{stock_total(p)}</td>
            <td>{safe(p.codigo_oem)}</td>
            <td>{safe(p.codigo_alternativo)}</td>
            <td>{safe(p.homologados)}</td>
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
