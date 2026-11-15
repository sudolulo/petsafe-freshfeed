"""Constants for the PetSafe FreshFeed integration (pure, no Home Assistant import)."""

DOMAIN = "petsafe_freshfeed"
SOURCE_DOMAIN = "tuya_local"
SOURCE_PROFILE = "petsafe_freshfeed_raw"
CONF_SOURCE_ENTRY_ID = "source_entry_id"
# Retained-intent store identity, persisted only after explicit provisioning
# succeeded. Absent means read-only; nothing ever creates or clears them on startup.
CONF_INTENT_STORE_ID = "intent_store_id"
CONF_INTENT_BINDING_ID = "intent_binding_id"
# Flow-only choice; never persisted (the IDs above are the persisted state).
CONF_ENABLE_CONTROLS = "enable_controls"
# Store root, relative to the HA configuration directory.
INTENT_STORE_ROOT = ".storage"
# Shutdown: quiesce the adapter, then drain transport before witness, each bounded.
LIFECYCLE_TIMEOUT = 5.0
# Bound on waiting for the owned connection to be current before enabling controls.
COORDINATOR_READY_TIMEOUT = 10.0
PLATFORMS = ("sensor", "binary_sensor", "event")
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
