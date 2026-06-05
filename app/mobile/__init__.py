from flask import Blueprint

mobile_bp = Blueprint(
    "mobile",
    __name__,
    url_prefix="/m",
    template_folder="../../templates",
    static_folder="../static/mobile",
    static_url_path="/static/mobile",
)

from . import routes  # noqa: E402, F401
