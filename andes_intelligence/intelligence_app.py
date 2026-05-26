from flask import Flask, Response, render_template, request, redirect
import pandas as pd
import os
import secrets
from difflib import SequenceMatcher
from pathlib import Path
from werkzeug.utils import secure_filename

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
ALLOWED_UPLOAD_EXTENSIONS = {".xlsx", ".xls"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

empresas = {}

# =========================
# FUNCIONES
# =========================

def cargar_excel(nombre_empresa, path):
    df = pd.read_excel(path)
    df.columns = df.columns.str.strip()
    df["clean"] = df.iloc[:,0].astype(str).str.lower()
    empresas[nombre_empresa] = df

def similitud(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _expected_auth_credentials():
    username = (os.environ.get("INTELLIGENCE_USERNAME") or "").strip()
    password = (os.environ.get("INTELLIGENCE_PASSWORD") or "").strip()
    if username and password:
        return username, password
    return None


def _auth_failed():
    return Response(
        "Autenticación requerida",
        401,
        {"WWW-Authenticate": 'Basic realm="Andes Intelligence"'},
    )


@app.before_request
def require_basic_auth():
    expected = _expected_auth_credentials()
    if expected is None:
        return _auth_failed()
    auth = request.authorization
    if not auth:
        return _auth_failed()
    if not (
        secrets.compare_digest(auth.username or "", expected[0])
        and secrets.compare_digest(auth.password or "", expected[1])
    ):
        return _auth_failed()


def _allowed_upload(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS

# =========================
# CARGA INICIAL
# =========================

if (BASE_DIR / "FITALIA.xlsx").exists():
    cargar_excel("Fitalia", str(BASE_DIR / "FITALIA.xlsx"))

if (BASE_DIR / "SOLUPARTS.xlsx").exists():
    cargar_excel("Soluparts", str(BASE_DIR / "SOLUPARTS.xlsx"))

# =========================
# HOME
# =========================

@app.route("/", methods=["GET"])
def home():

    q = request.args.get("q", "").lower()
    base = request.args.get("base")
    comp = request.args.get("comp")

    resultados = []

    if q and base in empresas and comp in empresas:

        df_base = empresas[base]
        df_comp = empresas[comp]

        base_filtrado = df_base[df_base["clean"].str.contains(q, na=False)].head(30)

        for _, row_b in base_filtrado.iterrows():

            desc_b = row_b.iloc[0]
            precio_b = float(row_b.iloc[-2]) if len(row_b) > 2 else 0

            mejor_score = 0
            mejor_match = None

            for _, row_c in df_comp.iterrows():
                desc_c = row_c.iloc[0]
                score = similitud(str(desc_b), str(desc_c))
                if score > mejor_score:
                    mejor_score = score
                    mejor_match = row_c

            if mejor_match is not None and mejor_score > 0.5:

                precio_c = float(mejor_match.iloc[-2]) if len(mejor_match) > 2 else 0
                diferencia = precio_b - precio_c
                score_pct = round(mejor_score * 100,1)

                resultados.append({
                    "descripcion": desc_b,
                    "precio_base": precio_b,
                    "precio_comp": precio_c,
                    "diferencia": round(diferencia,1),
                    "score": score_pct
                })

    return render_template(
        "intelligence.html",
        resultados=resultados,
        empresas=list(empresas.keys()),
        q=q
    )

# =========================
# SUBIR EMPRESA
# =========================

@app.route("/subir", methods=["POST"])
def subir():

    nombre = request.form["nombre"]
    archivo = request.files["archivo"]

    if archivo:
        if not _allowed_upload(archivo.filename or ""):
            return redirect("/")
        if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
            return redirect("/")
        safe_name = secure_filename(archivo.filename or "")
        if not safe_name:
            return redirect("/")
        path = UPLOAD_FOLDER / safe_name
        archivo.save(path)
        cargar_excel(nombre, str(path))

    return redirect("/")

# =========================
if __name__ == "__main__":
    app.run(debug=False)