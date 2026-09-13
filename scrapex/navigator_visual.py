"""Visual-frame binding for observation-bound coordinate actions.

A model may click a point on the screenshot it was shown. That click is only
safe if the page still looks the way it did where the click lands, so the
runtime keeps the unannotated viewport frame that went with each observation
and, before acting, compares the target-local region of that frame with the
same region rendered now. Whole-page equality is deliberately not required:
a ticking clock or a lazily loaded sidebar must not block a click on a print
icon that has not moved, while a menu that opened over the point must.

Dependency-free on purpose: Playwright hands back PNG bytes, and a PNG is a
zlib stream of filtered scanlines, which is small enough to decode here
without adding an imaging library to ScrapeX. Only the frames it actually
holds are decoded, and only on a visual action.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import zlib
from dataclasses import dataclass
from typing import Any, Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Half-width of the square compared around a visual target, in CSS pixels.
TARGET_REGION_RADIUS = 32
# Mean absolute grey difference (0-255) above which the region has changed.
# Two renders of the same static control differ by JPEG/anti-aliasing noise
# only, well under this; a menu, dialog, or scrolled page over the point
# differs by far more.
TARGET_REGION_MAX_DIFFERENCE = 22.0
MAX_CACHED_FRAMES = 12


@dataclass(frozen=True)
class GrayImage:
    width: int
    height: int
    pixels: bytes  # row-major, one byte per pixel

    def region(self, x: int, y: int, radius: int = TARGET_REGION_RADIUS) -> tuple[bytes, int, int]:
        """The grey pixels inside the square around (x, y), clipped to the image."""
        left = max(0, int(x) - radius)
        top = max(0, int(y) - radius)
        right = min(self.width, int(x) + radius)
        bottom = min(self.height, int(y) + radius)
        if right <= left or bottom <= top:
            return b"", 0, 0
        rows = []
        for row in range(top, bottom):
            start = row * self.width + left
            rows.append(self.pixels[start : start + (right - left)])
        return b"".join(rows), right - left, bottom - top


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png_gray(data: bytes) -> GrayImage:
    """Decode an 8-bit, non-interlaced PNG (grey, RGB, or with alpha) to grey."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    position = len(PNG_SIGNATURE)
    width = height = 0
    bit_depth = color_type = interlace = -1
    idat: list[bytes] = []
    while position + 8 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        chunk_type = data[position + 4 : position + 8]
        chunk = data[position + 8 : position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height = struct.unpack(">II", chunk[:8])
            bit_depth, color_type = chunk[8], chunk[9]
            interlace = chunk[12]
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if not width or not height or bit_depth != 8 or interlace != 0 or channels is None:
        raise ValueError("unsupported PNG layout")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ValueError("truncated PNG data")

    previous = bytearray(stride)
    gray = bytearray(width * height)
    offset = 0
    bpp = channels
    for row in range(height):
        filter_type = raw[offset]
        offset += 1
        current = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(bpp, stride):
                current[index] = (current[index] + current[index - bpp]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                current[index] = (current[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = current[index - bpp] if index >= bpp else 0
                current[index] = (current[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = current[index - bpp] if index >= bpp else 0
                up_left = previous[index - bpp] if index >= bpp else 0
                current[index] = (current[index] + _paeth(left, previous[index], up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unknown PNG filter {filter_type}")
        base = row * width
        if channels == 1:
            gray[base : base + width] = current
        elif channels == 2:
            gray[base : base + width] = current[0::2]
        else:
            for x in range(width):
                index = x * channels
                gray[base + x] = (
                    current[index] * 299 + current[index + 1] * 587 + current[index + 2] * 114
                ) // 1000
        previous = current
    return GrayImage(width=width, height=height, pixels=bytes(gray))


def region_difference(a: bytes, b: bytes) -> float:
    """Mean absolute difference between two equally sized grey regions."""
    if not a or not b or len(a) != len(b):
        return 255.0
    total = 0
    for left, right in zip(a, b):
        total += left - right if left >= right else right - left
    return total / len(a)


@dataclass(frozen=True)
class VisualFrame:
    task_id: str
    observation_id: str
    width: int
    height: int
    png: bytes
    sha256: str


class FrameCache:
    """Latest unannotated viewport frame per task, in memory only.

    Pixels are never written to Navigator state; a restart simply means a
    visual action must wait for a fresh observation and screenshot.
    """

    def __init__(self, capacity: int = MAX_CACHED_FRAMES):
        self._capacity = max(1, int(capacity))
        self._frames: dict[str, VisualFrame] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def put(self, task_id: str, observation_id: str, png: bytes, width: int, height: int) -> VisualFrame:
        frame = VisualFrame(
            task_id=task_id,
            observation_id=observation_id,
            width=int(width),
            height=int(height),
            png=bytes(png),
            sha256=hashlib.sha256(png).hexdigest(),
        )
        with self._lock:
            self._frames[task_id] = frame
            if task_id in self._order:
                self._order.remove(task_id)
            self._order.append(task_id)
            while len(self._order) > self._capacity:
                evicted = self._order.pop(0)
                self._frames.pop(evicted, None)
        return frame

    def get(self, task_id: str) -> Optional[VisualFrame]:
        with self._lock:
            return self._frames.get(task_id)


FRAMES = FrameCache()


def compare_target_region(
    reference_png: bytes,
    current_png: bytes,
    *,
    x: int,
    y: int,
    radius: int = TARGET_REGION_RADIUS,
) -> dict[str, Any]:
    """Compare the region around (x, y) in two viewport frames."""
    reference = decode_png_gray(reference_png)
    current = decode_png_gray(current_png)
    if (reference.width, reference.height) != (current.width, current.height):
        return {
            "same": False,
            "difference": None,
            "reason": "viewport size changed since the observation",
        }
    left_region, width, height = reference.region(x, y, radius)
    right_region, _, _ = current.region(x, y, radius)
    difference = region_difference(left_region, right_region)
    return {
        "same": difference <= TARGET_REGION_MAX_DIFFERENCE,
        "difference": round(difference, 2),
        "region": {"x": max(0, x - radius), "y": max(0, y - radius), "width": width, "height": height},
        "threshold": TARGET_REGION_MAX_DIFFERENCE,
    }
