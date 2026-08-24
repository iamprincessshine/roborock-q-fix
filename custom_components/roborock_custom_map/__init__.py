"""Roborock Custom Map integration."""

from __future__ import annotations

import json
import logging
from typing import Any

from roborock.callbacks import decoder_callback
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol

from homeassistant.components.roborock.coordinator import RoborockB01Q7UpdateCoordinator
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_MAP_ROTATION, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.IMAGE, Platform.SELECT]

# HA's own B01 messages use msg_ids >= 100000000000 (see get_next_int in
# python-roborock); the app/cloud uses small ids like 259704.
_APP_MSG_ID_LIMIT = 100000000000


def _parse_app_command(message: RoborockMessage) -> dict[str, Any] | None:
    """Extract a non-HA RPC command payload from an incoming message.

    Returns the inner command dict (method/msgId/params) or None if the
    message is not an app-sent command.
    """
    if message.protocol != RoborockMessageProtocol.RPC_REQUEST:
        return None
    try:
        data = json.loads(message.payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    for dps_value in (data.get("dps") or {}).values():
        if not isinstance(dps_value, str):
            continue
        try:
            inner = json.loads(dps_value)
        except json.JSONDecodeError:
            continue
        msg_id = str(inner.get("msgId", ""))
        if msg_id.isdigit() and int(msg_id) >= _APP_MSG_ID_LIMIT:
            continue  # our own command
        return inner
    return None


async def _capture_q7_app_commands(
    coordinator: RoborockB01Q7UpdateCoordinator,
) -> Any | None:
    """Log RPC commands the Roborock app sends to the device via the cloud.

    The app talks to the device through Roborock's cloud on the same MQTT
    topic HA publishes to (rr/m/i/...). Subscribing to it lets us see the
    app's commands in decrypted form. This is a debugging aid: the SC05
    firmware accepts the python-roborock Q7 room-clean format but ignores
    the room restriction, so we capture the real payload the app sends.
    """
    try:
        mqtt_channel = coordinator._device._channel._mqtt_channel
    except AttributeError:
        return None

    def on_message(message: RoborockMessage) -> None:
        if (inner := _parse_app_command(message)) is not None:
            _LOGGER.info("Q7 APP COMMAND: %s", inner)

    try:
        return await mqtt_channel._mqtt_session.subscribe(
            mqtt_channel._publish_topic,
            decoder_callback(mqtt_channel._decoder, on_message, _LOGGER),
        )
    except Exception:  # noqa: BLE001 - capture must never break setup
        _LOGGER.exception("Failed to subscribe to Q7 app command topic")
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Roborock Custom map from a config entry."""
    roborock_entries = hass.config_entries.async_entries("roborock")
    coordinators = []

    @callback
    def unload_this_entry() -> None:
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    for r_entry in roborock_entries:
        if r_entry.state == ConfigEntryState.LOADED:
            coordinators.extend(r_entry.runtime_data.v1)
            coordinators.extend(r_entry.runtime_data.b01_q7)
            r_entry.async_on_unload(unload_this_entry)

    if not coordinators:
        raise ConfigEntryNotReady("No Roborock entries loaded. Cannot start.")

    entry.runtime_data = coordinators

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(CONF_MAP_ROTATION, {})

    unsubs = []
    for coord in coordinators:
        if isinstance(coord, RoborockB01Q7UpdateCoordinator):
            if unsub := await _capture_q7_app_commands(coord):
                unsubs.append(unsub)
    if unsubs:
        hass.data[DOMAIN][entry.entry_id]["q7_capture_unsubs"] = unsubs

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    for unsub in hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("q7_capture_unsubs", []):
        unsub()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded