"""
WSGI entrypoint para un despliegue mínimo: solo login + búsqueda de productos (ANDES_APP_MODE=search_lite).

Render (o gunicorn):
  export ANDES_APP_MODE=search_lite
  gunicorn -w 1 -b 0.0.0.0:$PORT wsgi_search_lite:app

Importante: la variable de entorno debe existir *antes* de importar el paquete `app`
(por eso se setea en este módulo antes de `from app import app`).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANDES_APP_MODE", "search_lite")

from app import app  # noqa: E402  (import ordenado tras env)
