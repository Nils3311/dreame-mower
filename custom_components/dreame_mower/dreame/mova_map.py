"""Mova 600 Plus Map Renderer.

Standalone module for parsing and rendering Mova mower map data.
Works both as a local test script and as an HA integration module.

Map data comes from the `iotuserdata/getDeviceData` cloud endpoint.
It's stored as chunked JSON (MAP.0-MAP.N with MAP.info length header).
The map format uses vector polygons (not raster pixels like Dreame Vacuum).
"""

import io
import json
import logging
import math
from typing import Any

from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

# Colors (RGBA)
COLOR_BACKGROUND = (40, 40, 40, 255)
COLOR_CONTOUR_FILL = (60, 120, 60, 255)
COLOR_CONTOUR_OUTLINE = (80, 160, 80, 255)
COLOR_MOWING_AREA = (100, 180, 100, 255)
COLOR_MOWING_AREA_OUTLINE = (120, 200, 120, 255)
COLOR_FORBIDDEN_ZONE = (200, 60, 60, 180)
COLOR_FORBIDDEN_ZONE_OUTLINE = (255, 80, 80, 255)
COLOR_PATH = (144, 238, 144, 140)  # Light green semi-transparent (like app's mowed area)
COLOR_CHARGER = (0, 150, 255, 255)
COLOR_CHARGER_OUTLINE = (255, 255, 255, 255)
COLOR_ROBOT = (255, 200, 0, 255)
COLOR_ROBOT_OUTLINE = (255, 255, 255, 255)

# Path segment separator (pen-up marker)
PATH_PEN_UP_X = 32767
PATH_PEN_UP_Y = -32768

DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 800
DEFAULT_PADDING = 40


def parse_chunked_data(data: dict, prefix: str) -> str | None:
    """Reassemble chunked data (PREFIX.0, PREFIX.1, ...) using PREFIX.info as length.

    The Mova cloud stores large data split across multiple keys:
    - PREFIX.info = total character count
    - PREFIX.0, PREFIX.1, ... = chunks of max 1024 chars each
    """
    info_key = f"{prefix}.info"
    total_len = int(data.get(info_key, 0))
    if total_len == 0:
        return None

    chunks = {}
    dot_prefix = f"{prefix}."
    for key, val in data.items():
        if key.startswith(dot_prefix) and key != info_key:
            try:
                idx = int(key.split(".")[1])
                chunks[idx] = str(val)
            except (ValueError, IndexError):
                continue

    if not chunks:
        return None

    full_str = "".join(chunks[i] for i in sorted(chunks.keys()))
    return full_str[:total_len]


def parse_map_data(raw_data: dict) -> list[dict]:
    """Parse MAP chunks into a list of map objects.

    Returns list of map dicts, each with keys like:
    mowingAreas, forbiddenAreas, contours, boundary, name, mapIndex, etc.

    Gotcha 13: MAP chunks contain double-encoded JSON.
    The outer array contains JSON strings that need a second parse.
    """
    map_str = parse_chunked_data(raw_data, "MAP")
    if not map_str:
        _LOGGER.warning("No MAP data found in device data")
        return []

    try:
        maps_raw = json.loads(map_str)
    except json.JSONDecodeError as e:
        _LOGGER.error("Failed to parse MAP JSON: %s", e)
        return []

    maps = []
    for m in maps_raw:
        if isinstance(m, str):
            try:
                maps.append(json.loads(m))
            except json.JSONDecodeError:
                _LOGGER.warning("Failed to parse inner map JSON string")
                continue
        elif isinstance(m, dict):
            maps.append(m)

    return maps


def parse_path_data(raw_data: dict) -> list[list[tuple[int, int]]]:
    """Parse M_PATH chunks into a list of path segments.

    Each segment is a list of (x, y) coordinate tuples.
    Segments are separated by the pen-up marker [32767, -32768].

    Gotcha: M_PATH.info may be unreliable (set to 2 = "[]" even when
    M_PATH.1-15 contain real data). We try parse_chunked_data first,
    then fall back to extracting coordinates from all chunks via regex.
    """
    path_str = parse_chunked_data(raw_data, "M_PATH")

    path_data = None
    if path_str and len(path_str) > 2:
        try:
            # Try direct parse first (valid when M_PATH.info is correct)
            parsed = json.loads(path_str)
            if isinstance(parsed, list):
                path_data = parsed
        except json.JSONDecodeError:
            pass
        if path_data is None:
            try:
                # Fallback: wrap fragments in array brackets
                path_data = json.loads(f"[{path_str}]")
            except json.JSONDecodeError:
                pass

    # Fallback: M_PATH.info is unreliable — extract [x,y] pairs from all chunks
    if not path_data:
        import re
        all_chunks = []
        for key, val in raw_data.items():
            if key.startswith("M_PATH.") and key != "M_PATH.info":
                all_chunks.append((key, str(val)))

        if not all_chunks:
            return []

        # Sort by chunk index and combine
        def chunk_index(item):
            try:
                return int(item[0].split(".")[1])
            except (ValueError, IndexError):
                return 999
        all_chunks.sort(key=chunk_index)
        combined = "".join(v for _, v in all_chunks)

        # Extract all [x,y] coordinate pairs via regex
        pairs = re.findall(r'\[(-?\d+),\s*(-?\d+)\]', combined)
        if not pairs:
            return []

        # Convert to flat list for the standard parser below
        path_data = []
        for x_str, y_str in pairs:
            path_data.append([int(x_str), int(y_str)])
        _LOGGER.debug("M_PATH fallback: extracted %d coordinate pairs from chunks", len(path_data))

    segments = []
    current_segment: list[tuple[int, int]] = []
    i = 0

    while i < len(path_data):
        val = path_data[i]
        if val is None:
            i += 1
            continue

        if isinstance(val, list):
            if len(val) < 2:
                i += 1
                continue
            x, y = val[0], val[1]
            if x == PATH_PEN_UP_X and y == PATH_PEN_UP_Y:
                if current_segment:
                    segments.append(current_segment)
                current_segment = []
            else:
                current_segment.append((int(x), int(y)))
            i += 1
        elif isinstance(val, (int, float)):
            if i + 1 < len(path_data) and isinstance(path_data[i + 1], (int, float)):
                x, y = int(val), int(path_data[i + 1])
                if x == PATH_PEN_UP_X and y == PATH_PEN_UP_Y:
                    if current_segment:
                        segments.append(current_segment)
                    current_segment = []
                else:
                    current_segment.append((x, y))
                i += 2
            else:
                i += 1
        else:
            i += 1

    if current_segment:
        segments.append(current_segment)

    return segments


def parse_settings(raw_data: dict) -> Any | None:
    """Parse SETTINGS chunks into a settings object (may be dict or list)."""
    settings_str = parse_chunked_data(raw_data, "SETTINGS")
    if not settings_str:
        return None
    try:
        return json.loads(settings_str)
    except json.JSONDecodeError:
        return None


class MovaMapRenderer:
    """Renders Mova 600 Plus map data as PNG images.

    The Mova map format uses vector polygons (contours, mowing areas,
    forbidden zones) with coordinates in millimeters relative to the
    charging station origin (0, 0).
    """

    # Mower icon size on the map (pixels)
    ROBOT_ICON_SIZE = 45

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        padding: int = DEFAULT_PADDING,
    ):
        self._width = width
        self._height = height
        self._padding = padding
        self._last_md5: str | None = None
        self._cached_image: bytes | None = None
        self._robot_position: tuple[int, int] | None = None
        self._robot_heading: float = 0  # degrees, 0=up, CW positive
        self._robot_icon: Image.Image | None = None
        self._load_robot_icon()

    def _load_robot_icon(self) -> None:
        """Load and resize the mower icon PNG."""
        import os
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "Mova-600.png"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Mova-600.png"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                try:
                    icon = Image.open(path).convert("RGBA")
                    icon = icon.resize((self.ROBOT_ICON_SIZE, self.ROBOT_ICON_SIZE), Image.LANCZOS)
                    self._robot_icon = icon
                    return
                except Exception:
                    pass

    @property
    def image_width(self) -> int:
        return self._width

    @property
    def image_height(self) -> int:
        return self._height

    def set_robot_position(self, x: int, y: int) -> None:
        """Set the robot's current position in map coordinates (mm)."""
        new_pos = (x, y)
        if new_pos != self._robot_position:
            self._robot_position = new_pos
            self._cached_image = None

    def set_robot_heading(self, degrees: float) -> None:
        """Set the robot's heading in degrees (0=up/north, CW positive)."""
        if degrees != self._robot_heading:
            self._robot_heading = degrees
            self._cached_image = None

    def render(
        self,
        map_data: dict,
        path_segments: list[list[tuple[int, int]]] | None = None,
        robot_position: tuple[int, int] | None = None,
    ) -> bytes:
        """Render the map as a PNG image.

        Args:
            map_data: Parsed map dict (from parse_map_data()[0])
            path_segments: Parsed path segments (from parse_path_data())
            robot_position: Optional (x, y) position of robot in mm

        Returns:
            PNG image as bytes
        """
        md5 = map_data.get("md5sum", "")

        if md5 and md5 == self._last_md5 and self._cached_image:
            return self._cached_image

        boundary = map_data.get("boundary")
        if not boundary:
            _LOGGER.warning("Map has no boundary, returning empty image")
            return self._render_empty()

        x_min = boundary["x1"]
        y_min = boundary["y1"]
        x_max = boundary["x2"]
        y_max = boundary["y2"]
        x_range = x_max - x_min
        y_range = y_max - y_min

        if x_range <= 0 or y_range <= 0:
            return self._render_empty()

        w = self._width
        h = self._height
        pad = self._padding

        scale = min((w - 2 * pad) / x_range, (h - 2 * pad) / y_range)

        # Center the map in the image
        rendered_w = x_range * scale
        rendered_h = y_range * scale
        x_offset = (w - rendered_w) / 2
        y_offset = (h - rendered_h) / 2

        def to_screen(x: int | float, y: int | float) -> tuple[int, int]:
            sx = int((x - x_min) * scale + x_offset)
            sy = h - int((y - y_min) * scale + y_offset)  # Y-flip
            return (sx, sy)

        img = Image.new("RGBA", (w, h), COLOR_BACKGROUND)
        draw = ImageDraw.Draw(img)

        # Layer 1: Contours (garden outline)
        self._draw_areas(draw, map_data.get("contours", {}), to_screen,
                         COLOR_CONTOUR_FILL, COLOR_CONTOUR_OUTLINE)

        # Layer 2: Mowing areas (lawn)
        self._draw_areas(draw, map_data.get("mowingAreas", {}), to_screen,
                         COLOR_MOWING_AREA, COLOR_MOWING_AREA_OUTLINE)

        # Layer 3: Forbidden areas (no-go zones)
        self._draw_areas(draw, map_data.get("forbiddenAreas", {}), to_screen,
                         COLOR_FORBIDDEN_ZONE, COLOR_FORBIDDEN_ZONE_OUTLINE)

        # Layer 4: Mowing paths (filled mowed area)
        # M_PATH coordinates are in centimeters, map is in millimeters → scale by 10
        # Blade width ~180mm → calculate pixel width from scale
        if path_segments:
            blade_px = max(int(180 * scale), 2)  # 180mm blade width in pixels
            for segment in path_segments:
                if len(segment) >= 2:
                    screen_points = [to_screen(x * 10, y * 10) for x, y in segment]
                    draw.line(screen_points, fill=COLOR_PATH, width=blade_px)

        # Layer 5: Charging station at origin (0, 0)
        station = to_screen(0, 0)
        r = 8
        draw.ellipse(
            [station[0] - r, station[1] - r, station[0] + r, station[1] + r],
            fill=COLOR_CHARGER, outline=COLOR_CHARGER_OUTLINE,
        )

        # Layer 6: Robot position (use mower image rotated by heading, else dot)
        pos = robot_position or self._robot_position
        if pos:
            rp = to_screen(pos[0], pos[1])
            if self._robot_icon:
                # Rotate icon by heading (PIL rotates CCW, our heading is CW → negate)
                # Also Y is flipped on screen, so negate again → use heading as-is
                rotated = self._robot_icon.rotate(-self._robot_heading, expand=True, resample=Image.BICUBIC)
                rx, ry = rotated.size
                img.paste(rotated, (rp[0] - rx // 2, rp[1] - ry // 2), rotated)
            else:
                rr = 7
                draw.ellipse(
                    [rp[0] - rr, rp[1] - rr, rp[0] + rr, rp[1] + rr],
                    fill=COLOR_ROBOT, outline=COLOR_ROBOT_OUTLINE, width=2,
                )

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = buf.getvalue()

        self._last_md5 = md5
        self._cached_image = result

        return result

    def _draw_areas(
        self,
        draw: ImageDraw.ImageDraw,
        area_container: dict,
        to_screen,
        fill_color: tuple,
        outline_color: tuple,
    ) -> None:
        """Draw polygon areas (contours, mowing areas, forbidden zones)."""
        values = area_container.get("value", [])
        if not values:
            return

        for entry in values:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            area_data = entry[1]
            if not isinstance(area_data, dict) or "path" not in area_data:
                continue
            path = area_data["path"]
            points = [to_screen(p["x"], p["y"]) for p in path]
            if len(points) >= 3:
                draw.polygon(points, fill=fill_color, outline=outline_color)

    def _render_empty(self) -> bytes:
        """Render an empty placeholder image."""
        img = Image.new("RGBA", (self._width, self._height), COLOR_BACKGROUND)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class MovaMapManager:
    """Manages Mova map data fetching, parsing, and rendering.

    This replaces the DreameMapMowerMapManager for Mova devices.
    Mova stores map data as vector polygons in iotuserdata/getDeviceData,
    not as encrypted raster data in device properties.
    """

    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        self._renderer = MovaMapRenderer(width=width, height=height)
        self._raw_data: dict | None = None
        self._active_map: dict | None = None
        self._all_maps: list[dict] = []
        self._path_segments: list[list[tuple[int, int]]] = []
        self._accumulated_segments: list[list[tuple[int, int]]] = []  # across charge cycles
        self._prev_path_count: int = 0  # detect new session
        self._settings: dict | None = None
        self._image: bytes | None = None
        self._md5sum: str | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def image(self) -> bytes | None:
        return self._image

    @property
    def active_map(self) -> dict | None:
        return self._active_map

    @property
    def all_maps(self) -> list[dict]:
        return self._all_maps

    @property
    def settings(self) -> dict | None:
        return self._settings

    @property
    def md5sum(self) -> str | None:
        return self._md5sum

    @property
    def total_area(self) -> float:
        """Total mowing zone area in m²."""
        if self._active_map:
            areas = self._active_map.get("mowingAreas", {}).get("value", [])
            if areas and isinstance(areas[0], list) and len(areas[0]) >= 2:
                zone = areas[0][1]
                if isinstance(zone, dict):
                    return zone.get("area", 0)
        return 0

    @property
    def zone_mowing_time(self) -> int:
        """Total mowing time for the zone in seconds."""
        if self._active_map:
            areas = self._active_map.get("mowingAreas", {}).get("value", [])
            if areas and isinstance(areas[0], list) and len(areas[0]) >= 2:
                zone = areas[0][1]
                if isinstance(zone, dict):
                    return zone.get("time", 0)
        return 0

    @property
    def zone_effective_time(self) -> int:
        """Effective mowing time for the zone in seconds."""
        if self._active_map:
            areas = self._active_map.get("mowingAreas", {}).get("value", [])
            if areas and isinstance(areas[0], list) and len(areas[0]) >= 2:
                zone = areas[0][1]
                if isinstance(zone, dict):
                    return zone.get("etime", 0)
        return 0

    @property
    def mowed_area(self) -> float:
        """Estimated mowed area in m² using pixel-based coverage.

        Renders paths onto a low-res mask image and counts filled pixels.
        This correctly handles overlap (same area mowed twice = counted once).
        M_PATH coordinates in cm, map in mm → scale by 10.
        """
        if not self._path_segments or not self._active_map:
            return 0
        boundary = self._active_map.get("boundary")
        if not boundary:
            return 0

        x_min, y_min = boundary["x1"], boundary["y1"]
        x_max, y_max = boundary["x2"], boundary["y2"]
        x_range = x_max - x_min
        y_range = y_max - y_min
        if x_range <= 0 or y_range <= 0:
            return 0

        # Grid resolution 30mm for precision; 160mm cutting width (Mova 600 spec)
        res = 30
        blade_px = max(160 // res, 1)
        grid_w = max(x_range // res, 1)
        grid_h = max(y_range // res, 1)

        # Zone mask: mowing area polygon minus forbidden zones
        zone_mask = Image.new("1", (grid_w, grid_h), 0)
        zone_draw = ImageDraw.Draw(zone_mask)
        for entry in self._active_map.get("mowingAreas", {}).get("value", []):
            if isinstance(entry, list) and len(entry) >= 2:
                zone = entry[1]
                if isinstance(zone, dict) and "path" in zone:
                    pts = [((p["x"] - x_min) // res, (p["y"] - y_min) // res)
                           for p in zone["path"]]
                    if len(pts) >= 3:
                        zone_draw.polygon(pts, fill=1)
        for entry in self._active_map.get("forbiddenAreas", {}).get("value", []):
            if isinstance(entry, list) and len(entry) >= 2:
                zone = entry[1]
                if isinstance(zone, dict) and "path" in zone:
                    pts = [((p["x"] - x_min) // res, (p["y"] - y_min) // res)
                           for p in zone["path"]]
                    if len(pts) >= 3:
                        zone_draw.polygon(pts, fill=0)

        # Path mask
        path_mask = Image.new("1", (grid_w, grid_h), 0)
        path_draw = ImageDraw.Draw(path_mask)
        for segment in self._path_segments:
            if len(segment) >= 2:
                pts = [((x * 10 - x_min) // res, (y * 10 - y_min) // res)
                       for x, y in segment]
                path_draw.line(pts, fill=1, width=blade_px)

        # Intersect: only count mowed pixels inside the zone
        zone_data = zone_mask.getdata()
        path_data = path_mask.getdata()
        filled = sum(1 for z, p in zip(zone_data, path_data) if z and p)
        area_m2 = filled * (res * res) / 1_000_000
        # Correction factor: pixel-based calc consistently overestimates by ~12%
        # because repositioning/return paths inflate the mowed area.
        # Calibrated against MOVAhome app values across multiple sessions.
        area_m2 *= 0.89
        return round(area_m2, 1)

    @property
    def mowing_progress(self) -> float:
        """Estimated mowing progress percentage (mowed / total zone area)."""
        total = self.total_area
        if total <= 0:
            return 0
        return round(min(self.mowed_area / total * 100, 100), 1)

    def update(self, device_data: dict) -> bool:
        """Update map data from getDeviceData response.

        Args:
            device_data: The 'data' dict from getDeviceData API response

        Returns:
            True if map changed, False if unchanged
        """
        self._raw_data = device_data

        maps = parse_map_data(device_data)
        if not maps:
            _LOGGER.warning("No valid map data found")
            self._available = False
            return False

        self._all_maps = maps
        self._active_map = maps[0]

        new_md5 = self._active_map.get("md5sum", "")
        map_changed = new_md5 != self._md5sum
        self._md5sum = new_md5

        new_segments = parse_path_data(device_data)
        new_count = sum(len(s) for s in new_segments)

        # Accumulate paths across charge cycles:
        # If new data has FEWER points than last update, a new session started →
        # save previous session's paths and add new ones on top
        if new_count < self._prev_path_count and self._path_segments:
            self._accumulated_segments = list(self._path_segments)
        self._prev_path_count = new_count

        # Current view = accumulated (previous sessions) + current session
        self._path_segments = self._accumulated_segments + new_segments

        self._settings = parse_settings(device_data)
        self._available = True

        # Fallback robot position + heading from last M_PATH segment
        # M_PATH coords in cm, map in mm → ×10
        if self._path_segments and not self._renderer._robot_position:
            last_seg = self._path_segments[-1]
            if last_seg:
                lp = last_seg[-1]
                self._renderer.set_robot_position(lp[0] * 10, lp[1] * 10)
        # Compute heading from last 2 points of last segment
        if self._path_segments:
            last_seg = self._path_segments[-1]
            if len(last_seg) >= 2:
                p1, p2 = last_seg[-2], last_seg[-1]
                dx = p2[0] - p1[0]
                dy = p2[1] - p1[1]
                heading = math.degrees(math.atan2(-dx, dy))  # 0°=up, CW positive
                self._renderer.set_robot_heading(heading)

        self._render()

        return map_changed

    def set_robot_position(self, x: int, y: int) -> None:
        """Update robot position and re-render."""
        self._renderer.set_robot_position(x, y)
        if self._available:
            self._render()

    def _render(self) -> None:
        """Render the current map to PNG."""
        if not self._active_map:
            return
        try:
            self._image = self._renderer.render(
                self._active_map,
                self._path_segments,
            )
        except Exception as e:
            _LOGGER.error("Map render failed: %s", e)
            self._image = self._renderer._render_empty()


def render_map_from_device_data(device_data: dict, width: int = 800, height: int = 800) -> bytes:
    """One-shot convenience function: parse device data and render PNG.

    Args:
        device_data: The 'data' dict from getDeviceData API response
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        PNG image as bytes
    """
    mgr = MovaMapManager(width=width, height=height)
    mgr.update(device_data)
    return mgr.image or b""
