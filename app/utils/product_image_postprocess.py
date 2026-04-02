"""
Post-proceso de imágenes de producto: fondo blanco + marca de agua repetida.
Desactivar: variable de entorno ANDES_IMAGE_POSTPROCESS=0
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_WATERMARK_TEXT = "ANDES AUTO PARTS"


def _is_postprocess_enabled() -> bool:
    v = (os.environ.get("ANDES_IMAGE_POSTPROCESS") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _load_font(size: int) -> Any:
    candidates = []
    windir = os.environ.get("WINDIR")
    if windir:
        fonts = Path(windir) / "Fonts"
        candidates.extend(
            [
                fonts / "arial.ttf",
                fonts / "segoeui.ttf",
                fonts / "calibri.ttf",
            ]
        )
    candidates.extend(
        [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ]
    )
    for p in candidates:
        try:
            if p.exists():
                return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _flatten_to_white_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        base = img.convert("RGBA")
        bg.paste(base, mask=base.split()[3])
        return bg
    if img.mode == "CMYK":
        return img.convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _add_tiled_watermark_rgb(rgb: Image.Image) -> Image.Image:
    w, h = rgb.size
    if w < 32 or h < 32:
        return rgb

    base = rgb.convert("RGBA")
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_size = max(11, min(22, w // 28))
    font = _load_font(font_size)
    text = _WATERMARK_TEXT + "  ·  "
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = max(1, bbox[2] - bbox[0])
        th = max(1, bbox[3] - bbox[1])
    except Exception:
        tw, th = len(text) * 6, font_size + 4

    step_x = int(tw * 1.15) + 8
    step_y = int(th * 2.4) + 10
    fill = (160, 160, 160, 55)

    y = -step_y
    row = 0
    while y < h + step_y:
        x = -(step_x * 2) + (row % 2) * (step_x // 2)
        while x < w + step_x * 2:
            draw.text((x, y), text, font=font, fill=fill)
            x += step_x
        y += step_y
        row += 1

    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")


def process_uploaded_image(path: Path | str, *, quality: int = 90) -> Path | None:
    """
    Aplica fondo blanco (aplana alpha) + marca de agua. Guarda sobre el mismo archivo
    (JPEG/PNG/WebP). GIF se convierte a JPEG en un .jpg junto al nombre y se borra el .gif.

    Returns
    -------
    Path | None
        Ruta final del archivo, o None si está desactivado o hubo error.
    """
    if not _is_postprocess_enabled():
        return None

    path = Path(path)
    if not path.is_file():
        return None

    ext = path.suffix.lower()
    try:
        with Image.open(path) as im:
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            im = im.copy()
        rgb = _flatten_to_white_rgb(im)
        rgb = _add_tiled_watermark_rgb(rgb)

        if ext == ".gif":
            out = path.with_suffix(".jpg")
            rgb.save(out, format="JPEG", quality=quality, optimize=True)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return out

        if ext in (".jpg", ".jpeg"):
            rgb.save(path, format="JPEG", quality=quality, optimize=True)
        elif ext == ".png":
            rgb.save(path, format="PNG", optimize=True)
        elif ext == ".webp":
            rgb.save(path, format="WEBP", quality=quality)
        else:
            rgb.save(path, format="JPEG", quality=quality, optimize=True)
        return path
    except Exception:
        return None
