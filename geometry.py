from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


class GeometryError(ValueError):
    pass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int
    name: str = ""
    source: str = "manual"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], source: str | None = None) -> "Rect":
        try:
            x = _integer(value["x"], "x")
            y = _integer(value["y"], "y")
            width = _integer(value.get("w", value.get("width")), "width")
            height = _integer(value.get("h", value.get("height")), "height")
        except KeyError as exc:
            raise GeometryError(f"zone is missing coordinate {exc.args[0]}") from exc
        if width <= 0 or height <= 0:
            raise GeometryError("zone width and height must be positive")
        return cls(
            x=x,
            y=y,
            width=width,
            height=height,
            name=str(value.get("name", "")),
            source=source or str(value.get("source", "manual")),
        )

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeometryError(f"zone {label} must be an integer")
    return value


def chunk_intersects_rect(chunk_x: int, chunk_y: int, rect: Rect, chunk_size: int) -> bool:
    if chunk_size <= 0:
        raise GeometryError("chunk size must be positive")
    left = chunk_x * chunk_size
    top = chunk_y * chunk_size
    return (
        left < rect.right
        and rect.x < left + chunk_size
        and top < rect.bottom
        and rect.y < top + chunk_size
    )


def protected_chunks(
    chunks: Iterable[tuple[int, int]], rects: Iterable[Rect], chunk_size: int
) -> set[tuple[int, int]]:
    rect_list = list(rects)
    if chunk_size <= 0:
        raise GeometryError("chunk size must be positive")
    return {
        (chunk_x, chunk_y)
        for chunk_x, chunk_y in chunks
        if any(chunk_intersects_rect(chunk_x, chunk_y, rect, chunk_size) for rect in rect_list)
    }


def covered_grid_units(rects: Iterable[Rect], unit_size: int) -> set[tuple[int, int]]:
    """Return unique grid coordinates overlapped by one or more rectangles."""
    if unit_size <= 0:
        raise GeometryError("grid unit size must be positive")
    covered: set[tuple[int, int]] = set()
    for rect in rects:
        first_x, last_x = rect.x // unit_size, (rect.right - 1) // unit_size
        first_y, last_y = rect.y // unit_size, (rect.bottom - 1) // unit_size
        covered.update(
            (x, y)
            for x in range(first_x, last_x + 1)
            for y in range(first_y, last_y + 1)
        )
    return covered


def rectangles_from_export(export: Mapping[str, Any]) -> list[Rect]:
    zones = export.get("zones")
    if not isinstance(zones, list):
        raise GeometryError("zone export must contain a zones array")
    if any(not isinstance(zone, dict) for zone in zones):
        raise GeometryError("each exported zone must be an object")
    return [Rect.from_mapping(zone, source="live") for zone in zones]
