"""The visual-frame binding: PNG decoding, target-local comparison, frame cache."""

from __future__ import annotations

import struct
import zlib

import pytest

from scrapex.navigator_visual import (
    FrameCache,
    compare_target_region,
    decode_png_gray,
    region_difference,
)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def make_png(width: int, height: int, pixel, *, filters: tuple[int, ...] = (0,), channels: int = 3) -> bytes:
    """A small PNG whose pixel colour is ``pixel(x, y) -> (r, g, b)``."""
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    rows = []
    previous = bytes(width * channels)
    for y in range(height):
        raw = bytearray()
        for x in range(width):
            r, g, b = pixel(x, y)
            if channels == 1:
                raw += bytes([(r * 299 + g * 587 + b * 114) // 1000])
            elif channels == 3:
                raw += bytes([r, g, b])
            else:
                raw += bytes([r, g, b, 255])
        filter_type = filters[y % len(filters)]
        filtered = bytearray()
        for index, value in enumerate(raw):
            left = raw[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) >> 1
            else:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
            filtered.append((value - predictor) & 0xFF)
        rows.append(bytes([filter_type]) + bytes(filtered))
        previous = bytes(raw)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    body = zlib.compress(b"".join(rows))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", body) + _chunk(b"IEND", b"")


@pytest.mark.parametrize("filters", [(0,), (1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)])
@pytest.mark.parametrize("channels", [1, 3, 4])
def test_png_decodes_to_grey_under_every_filter(filters, channels):
    png = make_png(9, 7, lambda x, y: (x * 20, y * 30, 40), filters=filters, channels=channels)
    image = decode_png_gray(png)
    assert (image.width, image.height) == (9, 7)
    expected = (3 * 20 * 299 + 2 * 30 * 587 + 40 * 114) // 1000
    assert image.pixels[2 * 9 + 3] == expected


def test_non_png_and_unsupported_layouts_are_refused():
    with pytest.raises(ValueError):
        decode_png_gray(b"\xff\xd8\xffjpeg")
    with pytest.raises(ValueError):
        decode_png_gray(b"\x89PNG\r\n\x1a\n")


def test_region_difference_is_zero_for_identical_and_large_for_changed():
    assert region_difference(b"\x10\x20\x30", b"\x10\x20\x30") == 0
    assert region_difference(b"\x00\x00", b"\xff\xff") == 255.0
    assert region_difference(b"", b"") == 255.0
    assert region_difference(b"\x00", b"\x00\x00") == 255.0


def test_target_region_comparison_ignores_far_away_churn_but_catches_local_change():
    reference = make_png(120, 80, lambda x, y: (200, 200, 200))
    # A clock ticking in the corner changes nothing near the target.
    corner_churn = make_png(120, 80, lambda x, y: (0, 0, 0) if x > 110 and y < 8 else (200, 200, 200))
    same = compare_target_region(reference, corner_churn, x=40, y=40)
    assert same["same"] is True
    assert same["difference"] == 0

    # A menu opening over the point is a different target.
    covered = make_png(120, 80, lambda x, y: (10, 10, 10) if 20 <= x <= 60 and 20 <= y <= 60 else (200, 200, 200))
    changed = compare_target_region(reference, covered, x=40, y=40)
    assert changed["same"] is False
    assert changed["difference"] > changed["threshold"]


def test_target_region_comparison_refuses_a_resized_viewport():
    reference = make_png(60, 40, lambda x, y: (1, 2, 3))
    resized = make_png(61, 40, lambda x, y: (1, 2, 3))
    result = compare_target_region(reference, resized, x=10, y=10)
    assert result["same"] is False
    assert "viewport size changed" in result["reason"]


def test_frame_cache_keeps_the_latest_frame_per_task_and_evicts_oldest():
    cache = FrameCache(capacity=2)
    cache.put("t1", "obs_a", b"a", 10, 10)
    cache.put("t2", "obs_b", b"b", 10, 10)
    cache.put("t1", "obs_c", b"c", 10, 10)
    assert cache.get("t1").observation_id == "obs_c"
    cache.put("t3", "obs_d", b"d", 10, 10)
    assert cache.get("t2") is None
    assert cache.get("t1") is not None and cache.get("t3") is not None
    assert cache.get("t3").sha256
