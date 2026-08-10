"""Bootstrap del blueprint mobile — vive en C:\\App movil andes\\server."""
from flask import Blueprint

from app.utils.mobile_ui_paths import mobile_static_dir, mobile_ui_root

# Única fuente de la versión de la PWA. Al subirla se invalidan las cachés de
# CSS/JS (se inyecta como ?v= en las plantillas). Mantener SW_VERSION en
# static/service-worker.js con el mismo número.
PWA_VERSION = "v2026.08.10-v33"
ASSET_VERSION = PWA_VERSION.rsplit("-v", 1)[-1]

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
