"""Bridge: helpers de login PWA (implementación en andes_mobile/server)."""
from __future__ import annotations

import importlib.util
import sys

from app.utils.mobile_ui_paths import mobile_server_dir


def _load():
    path = mobile_server_dir() / "mobile_login.py"
    if not path.is_file():
        raise RuntimeError(f"No se encontró mobile_login en {path}")
    name = "andes_mobile_login_ext"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
MOBILE_PWA_COOKIE = _mod.MOBILE_PWA_COOKIE
is_mobile_login_context = _mod.is_mobile_login_context
mobile_login_target = _mod.mobile_login_target
