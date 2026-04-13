"""Constants for the OpenNeato integration."""

from homeassistant.components.vacuum import VacuumActivity

DOMAIN = "openneato"
CONF_HOST = "host"
DEFAULT_POLL_INTERVAL = 5  # seconds

# The firmware returns uiState as full enum strings like "UIMGR_STATE_HOUSECLEANINGRUNNING".
# We match using substrings (via .includes() style) like the frontend does in dashboard.tsx.
# These substring keys are checked against the raw uiState value.
UISTATE_SUBSTRINGS: list[tuple[str, VacuumActivity]] = [
    ("CLEANINGRUNNING", VacuumActivity.CLEANING),
    ("MANUALCLEANING", VacuumActivity.CLEANING),
    ("CLEANINGPAUSED", VacuumActivity.PAUSED),
    ("CLEANINGSUSPENDED", VacuumActivity.PAUSED),
    ("DOCKING", VacuumActivity.RETURNING),
]

FAN_SPEEDS = ["eco", "normal", "intense"]

# ── LIDAR map camera ────────────────────────────────────────────────
LIDAR_POLL_INTERVAL = 2  # seconds, only while robot is active
LIDAR_IMAGE_SIZE = 480  # pixels (square)
LIDAR_MAX_RANGE_MM = 5000  # display radius
LIDAR_MAX_DIST_MM = 6000  # reject readings above this
LIDAR_MAX_SCAN_AGE = 5  # keep points from the last N scans
LIDAR_MAX_BRIDGE_GAP = 5  # bridge up to N missing angles
LIDAR_MAX_DIST_JUMP_PCT = 0.3  # 30% — max jump to consider same surface
LIDAR_SMOOTH_WINDOW = 5  # moving-average half-window
LIDAR_MIN_SEGMENT_LEN = 3  # min points to draw a wall segment

# Colors (RGB tuples for PIL)
LIDAR_BG_COLOR = (30, 30, 34)  # #1E1E22
LIDAR_GRID_COLOR = (42, 42, 48)  # #2A2A30
LIDAR_WALL_COLOR = (91, 164, 245)  # #5BA4F5 — desaturated blue, colorblind-safe
LIDAR_ROBOT_COLOR = (138, 138, 142)  # #8A8A8E
