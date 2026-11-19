"""Constants for the PetSafe FreshFeed integration (pure, no Home Assistant import)."""

DOMAIN = "petsafe_freshfeed"
SOURCE_DOMAIN = "tuya_local"
CONF_SOURCE_ENTRY_ID = "source_entry_id"
# Explicit persisted connection backend; missing/unsupported values are refused.
CONF_CONNECTION_BACKEND = "connection_backend"
BACKEND_OWNED = "owned"
# Owned local connection fields (the key is never logged or displayed).
CONF_DEVICE_ID = "device_id"
CONF_LOCAL_KEY = "local_key"
CONF_HOST = "host"
CONF_PROTOCOL_VERSION = "protocol_version"
# Existing registry leaf of a migrated entry; new owned entries create theirs.
CONF_REGISTRY_DEVICE_ID = "registry_device_id"
# Inert historical identity marker: a missing persisted leaf must be refused.
CONF_MIGRATED_FROM = "migrated_from"
# Retained-intent store identity, persisted only after explicit provisioning
# succeeded. Absent means read-only; nothing ever creates or clears them on startup.
CONF_INTENT_STORE_ID = "intent_store_id"
CONF_INTENT_BINDING_ID = "intent_binding_id"
# hass.data[DOMAIN] key: entry IDs inside an explicit transition (host
# reconfigure) that unloads/sets up the entry itself; the update listener
# never reloads them meanwhile.
TRANSITIONS_KEY = "transitions"
# Flow-only choice; never persisted (the IDs above are the persisted state).
CONF_ENABLE_CONTROLS = "enable_controls"
# Store root, relative to the HA configuration directory.
INTENT_STORE_ROOT = ".storage"
# Shutdown: quiesce the adapter, then drain transport before witness, each bounded.
LIFECYCLE_TIMEOUT = 5.0
# Bound on waiting for the owned connection to be current before enabling controls.
COORDINATOR_READY_TIMEOUT = 10.0
PLATFORMS = ("sensor", "binary_sensor", "event", "switch", "number")
# role -> (entity domain, DP, historical unused unique-ID suffix).
# Preserve tuple shape; owned entities use <entry_id>_<role> identities.
OWNED_SETTING_ENTITIES = {
    "stainless_inserts": ("switch", "110", "-switch_stainless_inserts"),
    "meal_counter": ("switch", "125", "-switch_meal_counter"),
    "brightness_setting": ("number", "111", "-number_brightness"),
    "quick_feed_state": ("sensor", "140", "-sensor_quick_feed_state"),
}
# Writable setting DPs (role entities above) and their strict value domains.
SETTING_BOOL_DPS = frozenset(("110", "125"))
SETTING_BRIGHTNESS_DP = "111"
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 10
# Bounded window, from the send boundary, in which a later eligible report of
# the same DP with the requested value acknowledges a setting write.
SETTINGS_ACK_TIMEOUT = 5.0
EVENT_KINDS = ("dispensed", "refilled", "skipped_empty", "skipped_fault", "meal_report", "all_meals_report", "interrupted")

# Critical write operations and the exact single DP each may write. Feed is the
# quick-feed START intent token; the adapter selects the actual feed command.
WRITE_OPERATION_DPS = {"schedule": "1", "feed": "140", "cancel": "140", "defrost": "142", "refilled": "120"}
FEED_CANCEL_COMMAND = "IA=="
FEED_START_COMMAND = "EA=="
FEED_NOW_COMMAND = "MA=="
DEFROST_START_VALUE = 1
REFILLED_VALUE = True
WRITE_SEND_TIMEOUT = 5.0
RECOVERY_TIMEOUT = 35.0
