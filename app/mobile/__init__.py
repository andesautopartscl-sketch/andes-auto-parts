"""Andes Mobile — código y UI en andes_mobile/ (o C:\\App movil andes).

Este archivo es solo el puente del ERP: carga el paquete desde
``ANDES_MOBILE_ROOT/server`` o ``<repo>/andes_mobile/server``.
"""
from __future__ import annotations

from app.utils.mobile_ui_paths import mobile_server_dir

_SERVER = mobile_server_dir()

# Submódulos (routes, data, bootstrap, …) se cargan desde la carpeta externa
__path__ = [str(_SERVER)]  # type: ignore[misc]

from .bootstrap import mobile_bp  # noqa: E402

__all__ = ["mobile_bp"]
