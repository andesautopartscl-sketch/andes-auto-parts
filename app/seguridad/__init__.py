from flask import Blueprint

seguridad_bp = Blueprint(
    "seguridad",
    __name__,
    template_folder="templates"
)

from . import routes