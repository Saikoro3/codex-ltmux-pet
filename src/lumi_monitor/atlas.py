from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtGui import QImage

CELL_WIDTH = 192
CELL_HEIGHT = 208
ATLAS_WIDTH = CELL_WIDTH * 8
ATLAS_HEIGHT = CELL_HEIGHT * 11
USED_COLUMNS = (6, 8, 8, 4, 5, 8, 6, 6, 6, 8, 8)
# Codex v2 reserves idle column 6 for the neutral/front look pose.
EXTRA_USED_CELLS = {(0, 6)}


def validate_atlas(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    image = QImage(str(source))
    errors: list[str] = []
    if image.isNull():
        return {"ok": False, "path": str(source), "errors": ["image could not be decoded"]}
    if (image.width(), image.height()) != (ATLAS_WIDTH, ATLAS_HEIGHT):
        errors.append(f"expected {ATLAS_WIDTH}x{ATLAS_HEIGHT}, got {image.width()}x{image.height()}")
    if not image.hasAlphaChannel():
        errors.append("atlas has no alpha channel")
    if errors:
        return {"ok": False, "path": str(source), "errors": errors}

    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pointer = rgba.constBits()
    byte_count = rgba.bytesPerLine() * rgba.height()
    pointer.setsize(byte_count)
    pixels = memoryview(pointer)

    def cell_has_alpha(row: int, column: int) -> bool:
        left = column * CELL_WIDTH
        top = row * CELL_HEIGHT
        for y in range(top, top + CELL_HEIGHT):
            start = y * rgba.bytesPerLine() + left * 4 + 3
            if any(pixels[start : start + CELL_WIDTH * 4 : 4]):
                return True
        return False

    for row, used in enumerate(USED_COLUMNS):
        for column in range(8):
            populated = cell_has_alpha(row, column)
            expected = column < used or (row, column) in EXTRA_USED_CELLS
            if expected and not populated:
                errors.append(f"used cell {row}:{column} is empty")
            if not expected and populated:
                errors.append(f"unused cell {row}:{column} is not transparent")

    return {
        "ok": not errors,
        "path": str(source),
        "width": image.width(),
        "height": image.height(),
        "cell_width": CELL_WIDTH,
        "cell_height": CELL_HEIGHT,
        "rows": 11,
        "columns": 8,
        "errors": errors,
    }
