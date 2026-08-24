"""Support for Roborock image."""

from __future__ import annotations

import asyncio
from datetime import datetime
import io
import logging
from typing import Any, override

from PIL import Image, UnidentifiedImageError
from roborock.devices.traits.b01.q7.map_content import MapContent as Q7MapContent
from roborock.devices.traits.v1.home import HomeTrait
from roborock.devices.traits.v1.map_content import MapContent
from roborock.exceptions import RoborockException
from roborock.map.proto.b01_scmap_pb2 import RobotMap  # type: ignore[attr-defined]

from homeassistant.components.image import ImageEntity
from homeassistant.components.roborock.coordinator import (
    RoborockB01Q7UpdateCoordinator,
    RoborockDataUpdateCoordinator,
)
from homeassistant.components.roborock.entity import (
    RoborockCoordinatedEntityB01Q7,
    RoborockCoordinatedEntityV1,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MAP_ROTATION,
    DEFAULT_MAP_ROTATION,
    DOMAIN,
    MAP_ROTATION_OPTIONS,
    Q7_MAP_LIST_REFRESH_CYCLES,
    Q7_MAP_UPDATE_INTERVAL,
    SIGNAL_ROTATION_CHANGED,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return PNG (width, height) from raw bytes, or None if not a PNG."""
    if len(data) < 24:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def _rotate_point_map_xy(
    x: float, y: float, w: int, h: int, rotation: int
) -> tuple[float, float]:
    """Rotate a point in map pixel space around the image bounds.

    rotation is counter-clockwise (PIL Image.rotate does CCW).
    Uses continuous coordinates (w - x / h - y) to avoid off-by-one issues.
    """
    if rotation == 0:
        return (x, y)
    if rotation == 90:
        # CCW 90: new size (h, w)
        return (y, w - x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        # CCW 270 == CW 90: new size (h, w)
        return (h - y, x)
    return (x, y)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Roborock image platform."""
    v1_entities = [
        RoborockMap(
            config_entry,
            f"{coord.duid_slug}_custom_map_{map_info.name or f'Map {map_info.map_flag}'}",
            coord,
            coord.properties_api.home,
            map_info.map_flag,
            map_info.name,
        )
        for coord in config_entry.runtime_data
        if isinstance(coord, RoborockDataUpdateCoordinator)
        for map_info in (coord.properties_api.home.home_map_info or {}).values()
    ]
    q7_entities = [
        RoborockMapQ7(config_entry, coord)
        for coord in config_entry.runtime_data
        if isinstance(coord, RoborockB01Q7UpdateCoordinator)
    ]
    async_add_entities([*v1_entities, *q7_entities])


class RoborockMap(RoborockCoordinatedEntityV1, ImageEntity):
    """A class to let you visualize the map."""

    _attr_has_entity_name = True
    image_last_updated: datetime
    _attr_name: str

    def __init__(
        self,
        config_entry: ConfigEntry,
        unique_id: str,
        coordinator: RoborockDataUpdateCoordinator,
        home_trait: HomeTrait,
        map_flag: int,
        map_name: str,
    ) -> None:
        """Initialize a Roborock map."""
        RoborockCoordinatedEntityV1.__init__(self, unique_id, coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.map_flag = map_flag
        self.rotation_key = f"{coordinator.duid_slug}_{map_flag}"
        self._home_trait = home_trait

        if not map_name:
            map_name = f"Map {map_flag}"
        self._attr_name = f"{map_name}_custom"

        self.cached_map = b""
        self._raw_image_size: tuple[int, int] | None = None

        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_selected(self) -> bool:
        """Return if this map is the currently selected map."""
        return self.map_flag == self.coordinator.properties_api.maps.current_map

    @property
    def _map_content(self) -> MapContent | None:
        if self._home_trait.home_map_content and (
            map_content := self._home_trait.home_map_content.get(self.map_flag)
        ):
            return map_content
        return None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass load any previously cached maps from disk."""
        await super().async_added_to_hass()

        self._attr_image_last_updated = self.coordinator.last_home_update

        # Listen for rotation changes from the Select entity
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ROTATION_CHANGED}_{self.config_entry.entry_id}_{self.rotation_key}",
                self._handle_rotation_changed,
            )
        )

        self.async_write_ha_state()

    def _handle_rotation_changed(self) -> None:
        """Rotation changed; schedule state update in the event loop."""
        self.hass.loop.call_soon_threadsafe(self._async_handle_rotation_changed)


    @callback
    def _async_handle_rotation_changed(self) -> None:
        """Rotation changed; bump last_updated to bust the image cache."""
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update."""
        if (map_content := self._map_content) is None:
            return

        if self.cached_map != map_content.image_content:
            self.cached_map = map_content.image_content
            self._raw_image_size = _png_dimensions(self.cached_map)
            self._attr_image_last_updated = self.coordinator.last_home_update

        super()._handle_coordinator_update()

    def _rotate_image(self, raw: bytes, rotation: int) -> bytes:
        """Rotate image in executor thread."""
        img = Image.open(io.BytesIO(raw))
        img = img.rotate(rotation, expand=True)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def _get_rotation(self) -> int:
        """Get configured rotation for this map from hass.data (set by select entity)."""
        rotation = (
            self.hass.data.get(DOMAIN, {})
            .get(self.config_entry.entry_id, {})
            .get(CONF_MAP_ROTATION, {})
            .get(self.rotation_key, DEFAULT_MAP_ROTATION)
        )

        if rotation not in MAP_ROTATION_OPTIONS:
            _LOGGER.debug(
                "Unsupported map rotation %s, allowed values: %s, falling back to %s",
                rotation,
                MAP_ROTATION_OPTIONS,
                DEFAULT_MAP_ROTATION,
            )
            return DEFAULT_MAP_ROTATION

        return rotation

    async def async_image(self) -> bytes | None:
        """Get the image (with optional rotation)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        raw = map_content.image_content
        rotation = self._get_rotation()

        if rotation == DEFAULT_MAP_ROTATION:
            return raw

        try:
            return await self.hass.async_add_executor_job(
                self._rotate_image, raw, rotation
            )
        except (OSError, UnidentifiedImageError) as err:
            _LOGGER.debug(
                "Failed to rotate Roborock map image: %s, returning original image",
                err,
            )
            return raw

    @property
    def extra_state_attributes(self):
        """Return extra attributes for map card usage (rotation-aware calibration)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        map_data = map_content.map_data
        if map_data is None:
            return {}

        # Attach room names (same behavior as before)
        if map_data.rooms is not None:
            for room in map_data.rooms.values():
                name = self._home_trait._rooms_trait.room_map.get(room.number)
                room.name = name.name if name else "Unknown"

        calibration = map_data.calibration()

        # Rotate ONLY the "map" (pixel-space) side of calibration points.
        # Rooms/zones are in vacuum coordinate space and are mapped via calibration.
        rotation = self._get_rotation()
        size = self._raw_image_size
        if rotation != DEFAULT_MAP_ROTATION and size is not None:
            w, h = size
            rotated_calibration = []
            for pt in calibration:
                mp = pt.get("map") or {}
                x = mp.get("x")
                y = mp.get("y")

                # If missing/invalid, keep point as-is
                if x is None or y is None:
                    rotated_calibration.append(pt)
                    continue

                nx, ny = _rotate_point_map_xy(float(x), float(y), w, h, rotation)

                new_pt = dict(pt)
                new_map = dict(mp)
                new_map["x"] = nx
                new_map["y"] = ny
                new_pt["map"] = new_map
                rotated_calibration.append(new_pt)

            calibration = rotated_calibration

        return {
            "calibration_points": calibration,
            "rooms": map_data.rooms,
            "zones": map_data.zones,
        }


class RoborockMapQ7(RoborockCoordinatedEntityB01Q7, ImageEntity):
    """Map image entity for B01 Q7 / SC05 devices.

    Q7 devices have no push-based map updates: the map is fetched on request
    via `service.upload_by_mapid`, so this entity polls `api.map_content` on
    its own interval and exposes the rendered PNG plus room names for the
    Xiaomi Vacuum Map Card.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Map_custom"

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockB01Q7UpdateCoordinator,
    ) -> None:
        """Initialize a Q7 map."""
        RoborockCoordinatedEntityB01Q7.__init__(
            self, f"{coordinator.duid_slug}_custom_map_q7", coordinator
        )
        ImageEntity.__init__(self, coordinator.hass)
        self.config_entry = config_entry
        self._api = coordinator.api
        self._cached_map: bytes | None = None
        self._unsub_timer: Any | None = None
        self._refresh_lock = asyncio.Lock()
        self._cycles = 0

    @override
    async def async_added_to_hass(self) -> None:
        """Start polling the map."""
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh_map())
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_refresh_map, Q7_MAP_UPDATE_INTERVAL
        )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop polling the map."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        await super().async_will_remove_from_hass()

    async def _async_refresh_map(self, _now: Any | None = None) -> None:
        """Fetch the current map from the device and update the image."""
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            try:
                if (
                    self._api.map.current_map_id is None
                    or self._cycles % Q7_MAP_LIST_REFRESH_CYCLES == 0
                ):
                    await self._api.map.refresh()
                await self._api.map_content.refresh()
            except RoborockException as err:
                _LOGGER.debug("Failed to refresh Q7 map: %s", err)
                return
            finally:
                self._cycles += 1

        content: Q7MapContent = self._api.map_content
        if (
            content.image_content is not None
            and content.image_content != self._cached_map
        ):
            self._cached_map = content.image_content
            self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @override
    async def async_image(self) -> bytes | None:
        """Get the cached image."""
        return self._cached_map

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose map data for the Xiaomi Vacuum Map Card."""
        map_data = self._api.map_content.map_data
        attrs: dict[str, Any] = {}
        if map_data is None:
            return attrs
        if room_names := map_data.additional_parameters.get("room_names"):
            attrs["room_names"] = room_names
        # ponytail: calibration derived from SCMap mapHead; semantics guessed
        # from grid rendering (FLIP_TOP_BOTTOM), verify against a live payload
        # before relying on room/robot overlays.
        if (calibration := _q7_calibration_points(self._api)) is not None:
            attrs["calibration_points"] = calibration
        return attrs


def _q7_calibration_points(api: Any) -> list[dict[str, Any]] | None:
    """Compute pixel<->vacuum calibration from the raw SCMap payload."""
    raw = api.map_content.raw_api_response
    if not raw:
        return None
    parsed = RobotMap()
    try:
        parsed.ParseFromString(raw)
    except Exception:  # noqa: BLE001 - protobuf raises DecodeError/ValueError
        return None
    head = parsed.mapHead
    if not (
        head.HasField("sizeX")
        and head.HasField("sizeY")
        and head.HasField("minX")
        and head.HasField("minY")
        and head.HasField("maxX")
        and head.HasField("maxY")
    ):
        return None
    size_x, size_y = head.sizeX, head.sizeY
    if not size_x or not size_y:
        return None

    def to_vacuum(px: float, py: float) -> dict[str, float]:
        # Image is rendered FLIP_TOP_BOTTOM, so pixel row 0 == maxY.
        return {
            "x": head.minX + px * (head.maxX - head.minX) / size_x,
            "y": head.maxY - py * (head.maxY - head.minY) / size_y,
        }

    return [
        {"vacuum": to_vacuum(0, 0), "map": {"x": 0, "y": 0}},
        {"vacuum": to_vacuum(size_x, 0), "map": {"x": size_x, "y": 0}},
        {"vacuum": to_vacuum(0, size_y), "map": {"x": 0, "y": size_y}},
    ]
