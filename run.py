from pathlib import Path
import os
import sys


EXPECTED_VENV_PYTHON = str((Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe").resolve())


def _ensure_expected_interpreter() -> None:
    current_python = str(Path(sys.executable).resolve())
    # No forzamos un path fijo: en Windows puede variar según dónde se haya creado el venv.
    # Si hay un venv esperado y no se está usando, avisamos pero dejamos correr.
    if os.name != "nt":
        return

    expected_exists = Path(EXPECTED_VENV_PYTHON).exists()
    in_venv = (getattr(sys, "base_prefix", sys.prefix) != sys.prefix) or bool(os.environ.get("VIRTUAL_ENV"))

    if expected_exists and current_python.lower() != EXPECTED_VENV_PYTHON.lower():
        print(
            "WARN: Se recomienda correr con este intérprete para evitar inconsistencias:\n"
            f"  Esperado: {EXPECTED_VENV_PYTHON}\n"
            f"  Actual:   {current_python}\n"
            "Continuando igualmente..."
        )
    elif not in_venv:
        print(
            "WARN: Estás corriendo fuera de un entorno virtual (venv). "
            "Si faltan dependencias, crea/activa un venv e instala requirements."
        )


_ensure_expected_interpreter()

from app import create_app

app = create_app()

print("DB ACTUAL:", app.config["SQLALCHEMY_DATABASE_URI"])

if __name__ == "__main__":
    app.run(
    host="127.0.0.1",
    port=5000,
    debug=True
)