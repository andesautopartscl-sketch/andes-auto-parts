"""Bootstrap del blueprint mobile — vive en C:\\App movil andes\\server."""
from flask import Blueprint

from app.utils.mobile_ui_paths import mobile_static_dir, mobile_ui_root

_root = mobile_ui_root()
mobile_bp = Blueprint(
    "mobile",
    "app.mobile",
    url_prefix="/m",
    static_folder=str(mobile_static_dir()),
    static_url_path="/static/mobile",
    root_path=str(_root) if _root else None,
)

from . import routes  # noqa: E402, F401
