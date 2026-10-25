"""原图保存，以及返回给客户端之前的缩小。"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image as PILImage
from PIL import UnidentifiedImageError

CLIENT_MAX_EDGE = 768


def save_original(data: bytes, dest_stem: Path) -> Path:
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    if data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        suffix = ".img"
    path = dest_stem.with_suffix(suffix)
    path.write_bytes(data)
    return path


def to_client_jpeg(data: bytes, max_edge: int = CLIENT_MAX_EDGE) -> bytes:
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            converted = image.convert("RGB")
            converted.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法读取图片") from exc


def jpeg_size(data: bytes) -> tuple[int, int]:
    with PILImage.open(io.BytesIO(data)) as image:
        return image.size
