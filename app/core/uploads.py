"""Safe handling of uploaded images (product photos, expense attachments).

Files are validated by real image content (Pillow), re-encoded to strip any
embedded payload, down-scaled to a sane maximum, and stored under
``app/static/uploads/<subdir>/`` with a random name. The returned path is
relative to ``static/`` so templates can serve it with ``url_for('static', ...)``.
"""
from __future__ import annotations

import os
import secrets

from flask import current_app

_ALLOWED = {"png", "jpg", "jpeg", "gif", "webp"}
_MAX_SIDE = 1200  # px — down-scale larger images


def _uploads_root() -> str:
    return os.path.join(current_app.root_path, "static", "uploads")


def save_image(file_storage, subdir: str) -> str | None:
    """Validate + store an uploaded image. Returns the path relative to static/,
    or None if no/invalid file was supplied."""
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in \
        file_storage.filename else ""
    if ext not in _ALLOWED:
        raise ValueError("صيغة صورة غير مدعومة (المسموح: PNG, JPG, GIF, WEBP).")

    from PIL import Image  # lazy import
    try:
        img = Image.open(file_storage.stream)
        img.verify()  # detect truncated / non-image content
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("تعذّر قراءة الصورة — تأكد أنها ملف صورة صحيح.") from exc

    img = img.convert("RGB")
    img.thumbnail((_MAX_SIDE, _MAX_SIDE))

    folder = os.path.join(_uploads_root(), subdir)
    os.makedirs(folder, exist_ok=True)
    name = f"{secrets.token_hex(8)}.jpg"
    img.save(os.path.join(folder, name), format="JPEG", quality=85)
    return f"uploads/{subdir}/{name}"


def delete_image(rel_path: str | None) -> None:
    """Remove a previously stored image (best effort)."""
    if not rel_path:
        return
    try:
        full = os.path.join(current_app.root_path, "static", rel_path)
        if os.path.isfile(full):
            os.remove(full)
    except OSError:  # pragma: no cover
        pass
