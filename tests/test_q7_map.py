"""Smoke test for the Q7/SC05 map entity in roborock_custom_map.

Stubs out `homeassistant.*` (not installed here) and uses the real
python-roborock library to verify:

1. `async_setup_entry` creates a `RoborockMapQ7` for a Q7 coordinator
   and does not crash on the V1-only rotation select path.
2. `_q7_calibration_points` derives sane pixel<->vacuum points from a
   synthetic SCMap payload.

Run:  python -m pytest tests/test_q7_map.py -x  (or python tests/test_q7_map.py)
"""

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

# --- Stub homeassistant modules so image.py imports without HA installed ---


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class FakeImageEntity:
    def __init__(self, hass=None):
        self.hass = hass
        self._attr_image_last_updated = None

    def async_write_ha_state(self):
        pass

    async def async_image(self):
        raise NotImplementedError


class FakeCoordinatedB01Q7:
    """Mirror of RoborockCoordinatedEntityB01Q7 init contract."""

    def __init__(self, unique_id, coordinator):
        self._attr_unique_id = unique_id
        self.coordinator = coordinator


_stub_module("homeassistant")
_stub_module("homeassistant.util")
_stub_module("homeassistant.util.dt", utcnow=MagicMock(return_value="now"))
_stub_module(
    "homeassistant.const",
    EntityCategory=type("EC", (), {"DIAGNOSTIC": 1}),
    Platform=type("P", (), {"IMAGE": 1, "SELECT": 2}),
)
_stub_module(
    "homeassistant.core",
    HomeAssistant=object,
    callback=lambda f: f,
)
_stub_module(
    "homeassistant.exceptions",
    HomeAssistantError=Exception,
    ConfigEntryNotReady=Exception,
)
_stub_module(
    "homeassistant.config_entries",
    ConfigEntry=object,
    ConfigEntryState=type("S", (), {"LOADED": 1}),
)
_stub_module(
    "homeassistant.helpers",
)
_stub_module(
    "homeassistant.helpers.event",
    async_track_time_interval=lambda *a, **k: lambda: None,
)
_stub_module(
    "homeassistant.helpers.dispatcher",
    async_dispatcher_connect=lambda *a, **k: lambda: None,
)
_stub_module(
    "homeassistant.helpers.entity_platform",
    AddConfigEntryEntitiesCallback=object,
)
_stub_module(
    "homeassistant.components",
)
_stub_module("homeassistant.components.image", ImageEntity=FakeImageEntity)
_stub_module(
    "homeassistant.components.roborock",
)
_stub_module(
    "homeassistant.components.roborock.coordinator",
    RoborockDataUpdateCoordinator=type("RDUC", (), {}),
    RoborockB01Q7UpdateCoordinator=type("RBQ7", (), {}),
)
import homeassistant.components.roborock.coordinator as _rr_coord  # noqa: E402
_stub_module(
    "homeassistant.components.roborock.entity",
    RoborockCoordinatedEntityV1=type("RCV1", (), {}),
    RoborockCoordinatedEntityB01Q7=FakeCoordinatedB01Q7,
)

sys.path.insert(0, "custom_components")
from roborock_custom_map import image as image_mod  # noqa: E402
from roborock_custom_map.image import RoborockMapQ7, _q7_calibration_points  # noqa: E402


class FakeMapTrait:
    current_map_id = None
    refresh_calls = 0

    async def refresh(self):
        self.refresh_calls += 1
        self.current_map_id = 7


class FakeMapContentTrait:
    def __init__(self):
        self.image_content = None
        self.map_data = None
        self.raw_api_response = None
        self.refresh_calls = 0

    async def refresh(self):
        self.refresh_calls += 1
        self.image_content = b"\x89PNG\r\n\x1a\n" + b"0" * 24


class FakeApi:
    def __init__(self):
        self.map = FakeMapTrait()
        self.map_content = FakeMapContentTrait()


class FakeQ7Coordinator(_rr_coord.RoborockB01Q7UpdateCoordinator):
    def __init__(self):
        self.duid_slug = "khui"
        self.api = FakeApi()
        self.device_info = {}
        self.hass = MagicMock()
        self.hass.async_create_task = lambda coro: None

    @property
    def properties_api(self):
        return self.api


@pytest.mark.asyncio
async def test_setup_creates_q7_entity_only():
    coord = FakeQ7Coordinator()
    entry = MagicMock()
    entry.runtime_data = [coord]
    added = []

    await image_mod.async_setup_entry(MagicMock(), entry, added.append)

    assert len(added) == 1
    assert len(added[0]) == 1  # async_add_entities receives a list
    entity = added[0][0]
    assert isinstance(entity, RoborockMapQ7)
    assert entity._attr_unique_id == "khui_custom_map_q7"


@pytest.mark.asyncio
async def test_refresh_fetches_map_and_caches_image():
    coord = FakeQ7Coordinator()
    entity = RoborockMapQ7(MagicMock(), coord)

    await entity._async_refresh_map()

    assert coord.api.map_content.refresh_calls == 1
    assert entity._cached_map == coord.api.map_content.image_content
    assert coord.api.map.refresh_calls == 1  # first cycle: map list fetched


@pytest.mark.asyncio
async def test_refresh_skips_map_list_on_subsequent_cycles():
    coord = FakeQ7Coordinator()
    coord.api.map.current_map_id = 7  # already known
    entity = RoborockMapQ7(MagicMock(), coord)
    entity._cycles = 5  # not a multiple of 10

    await entity._async_refresh_map()

    assert coord.api.map.refresh_calls == 0
    assert coord.api.map_content.refresh_calls == 1


def test_calibration_points_from_scmap_header():
    from roborock.map.proto.b01_scmap_pb2 import RobotMap

    rm = RobotMap()
    h = rm.mapHead
    h.sizeX, h.sizeY = 100, 50
    h.minX, h.minY, h.maxX, h.maxY = 0.0, 0.0, 100.0, 50.0

    class FakeContent:
        raw_api_response = rm.SerializeToString()

    class FakeApi:
        map_content = FakeContent()

    pts = _q7_calibration_points(FakeApi())
    assert pts[0]["vacuum"] == {"x": 0.0, "y": 50.0}
    assert pts[1]["vacuum"] == {"x": 100.0, "y": 50.0}
    assert pts[2]["vacuum"] == {"x": 0.0, "y": 0.0}
    assert pts[1]["map"] == {"x": 100, "y": 0}


def test_calibration_none_without_payload():
    class FakeApi:
        map_content = type("C", (), {"raw_api_response": None})()

    assert _q7_calibration_points(FakeApi()) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-x"]))
