"""Constants for Roborock Custom Map integration."""

from datetime import timedelta

DOMAIN = "roborock_custom_map"

CONF_MAP_ROTATION = "map_rotation"
DEFAULT_MAP_ROTATION = 0
MAP_ROTATION_OPTIONS = (0, 90, 180, 270)

SIGNAL_ROTATION_CHANGED = "roborock_custom_map_rotation_changed"

# Q7/SC05 devices have no push-based map updates; the map is fetched on
# request (`service.upload_by_mapid`), so the image entity polls it.
Q7_MAP_UPDATE_INTERVAL = timedelta(seconds=30)
# Refresh the map list (get_map_list) every N polls so a map switch made in
# the Roborock app is picked up without hammering the device.
Q7_MAP_LIST_REFRESH_CYCLES = 10
