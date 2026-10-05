"""Drive LIDAR mapping across a cleaning run.

Watches the coordinator for the robot going out to clean, samples pose and
scan while it works, and folds the result into the accumulated map when it
docks. The map improves with every run instead of being rebuilt from one.

The polling budget is deliberately modest. Reading a scan holds the robot's
serial link for roughly 800 ms, and the firmware skips its own 2 s pose
snapshot while a fetch is in flight, so this watches the recording session
file's growth and backs off if the robot's own path log starts thinning.
Measured over a 49-minute run at a 4 s interval, that log stayed at 95-126%
of its normal rate.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from datetime import timedelta

from .const import DOMAIN
from .lidar_mapper import (
    plan_calibration,
    AccumulatedMap,
    build_session_grids,
    parse_pose,
    render_plan,
    scan_points,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
POLL_INTERVAL = 4.0          # seconds between captures
# A scan is only geometry if the laser was actually sweeping. Rather than pin a
# nominal speed -- the robot reports 5.03 while docked and the field's unit is
# not documented -- each run is judged against its own median: anything under
# this fraction of it was taken while the LDS was spinning up, stalling or
# coasting, and its 360 "angles" were never swept in one revolution.
MIN_ROTATION_FRACTION = 0.6
# Movement tolerated between the poses bracketing a scan.
MAX_MOVE_DURING_SCAN_M = 0.12
MAX_TURN_DURING_SCAN_DEG = 25.0
MIN_CAPTURES = 60            # below this a run is too thin to be worth merging
BYTES_PER_POSE = 48          # firmware writes ~48 bytes per snapshot, every 2 s
MAX_INTERVAL = 12.0

# Health is judged on growth since collection began, never window to window.
#
# The firmware buffers pose lines and flushes to flash every 30 s, so a short
# window catches one flush or three depending on where it lands. Measuring the
# same thing over 60 s windows on a workstation read 97%, then 51%, then 142%
# across three consecutive minutes while the robot was logging perfectly.
# Judging a cumulative average removes that aliasing, and a grace period keeps
# the first minutes -- when a single flush dominates -- from counting at all.
#
# The check can now only slow sampling down, never stop it. Stopping on a
# measurement this noisy silently threw away the rest of a run's captures.
HEALTH_EVERY = 300.0
HEALTH_GRACE = 600.0
BACKOFF_RATIO = 0.55

# uiState substrings, matching camera.py.
CLEANING = ("CLEANINGRUNNING", "CLEANINGPAUSED", "CLEANINGSUSPENDED", "DOCKING")
ACTIVE = ("CLEANINGRUNNING",)


class LidarMapRunner:
    """Collects scans during a clean and maintains the accumulated map."""

    def __init__(self, hass: HomeAssistant, entry_id: str, api, coordinator) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.api = api
        self.coordinator = coordinator
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_lidar_map")
        self._map: AccumulatedMap | None = None
        self._captures: list[tuple[float, float, float, list[tuple[int, int]]]] = []
        self._collecting = False
        self._unsub_timer = None
        self._unsub_coordinator = None
        self._interval = POLL_INTERVAL
        self._busy = False
        self._last_health = 0.0
        self._health_start = 0.0
        self._health_ref: dict[str, Any] | None = None
        self._session_name: str | None = None
        self.last_report: dict[str, Any] = {}

    async def async_load(self) -> None:
        """Restore the accumulated map from storage."""
        data = await self._store.async_load()
        self._map = AccumulatedMap(data)
        if self._map.sessions:
            _LOGGER.info(
                "LIDAR map restored: %d wall cells from %d cleanings",
                len(self._map.walls), self._map.sessions,
            )
        self._unsub_coordinator = self.coordinator.async_add_listener(self._handle_update)

    @callback
    def async_unload(self) -> None:
        self._stop_timer()
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None

    # ── lifecycle ───────────────────────────────────────────────────

    @callback
    def _handle_update(self) -> None:
        state = ((self.coordinator.data or {}).get("state") or {}).get("uiState", "")
        cleaning = any(s in state for s in CLEANING)
        if cleaning and not self._collecting:
            self._start()
        elif not cleaning and self._collecting:
            self.hass.async_create_task(self._finish())

    def _start(self) -> None:
        self._collecting = True
        self._captures = []
        self._interval = POLL_INTERVAL
        self._last_health = time.monotonic()
        self._health_start = time.monotonic()
        self._health_ref = self._recording_session()
        # Remember which file this run is writing to, so the alignment worked
        # out at merge time can be stored against it and replayed in the same
        # frame as the map. Captured at the start because by the time the run
        # ends the robot has stopped reporting it as recording.
        self._session_name = (self._health_ref or {}).get("name")
        self._start_timer()
        _LOGGER.info("LIDAR mapping: collection started")

    def _start_timer(self) -> None:
        self._stop_timer()
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=self._interval)
        )

    def _stop_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # ── sampling ────────────────────────────────────────────────────

    async def _async_tick(self, _now=None) -> None:
        if self._session_name is None:
            # History is on a slower cadence than state, so the recording
            # entry may not have been listed yet when _start() ran. Backfill
            # it here so the alignment is stored against the right file.
            self._session_name = (self._recording_session() or {}).get("name")
        if self._busy:
            return
        state = ((self.coordinator.data or {}).get("state") or {}).get("uiState", "")
        if not any(s in state for s in ACTIVE):
            # Paused or heading for the dock: no new floor is being laid, so
            # spend nothing on the serial link.
            return
        self._busy = True
        try:
            # Pose first -- the scan read is the slow half, so this timestamp
            # sits closest to the scan's own instant.
            raw = await self.api.send_serial_command("GetRobotPos Smooth")
            pose = parse_pose(str(raw))
            payload = await self.api.get_lidar()
            points = scan_points(payload)
            # Pose again, after the scan. A scan is a ~800 ms serial round
            # trip, so the pose taken before it is that far stale by the time
            # the beam data actually lands; pairing them projects every return
            # from where the robot *was*. Bracketing the scan gives both ends,
            # and the midpoint is the honest estimate of where it was mid-scan.
            raw_after = await self.api.send_serial_command("GetRobotPos Smooth")
            pose_after = parse_pose(str(raw_after))
            if pose and points:
                x, y, theta, _ts = pose
                if pose_after:
                    x2, y2, theta2, _ts2 = pose_after
                    moved = math.hypot(x2 - x, y2 - y)
                    turned = abs((theta2 - theta + 180.0) % 360.0 - 180.0)
                    if moved > MAX_MOVE_DURING_SCAN_M or turned > MAX_TURN_DURING_SCAN_DEG:
                        # The frame shifted under the beam. One scan smeared
                        # across two positions is worse than no scan at all,
                        # because it lays walls that were never there.
                        _LOGGER.debug(
                            "LIDAR mapping: scan dropped, robot moved %.2f m / %.0f deg during it",
                            moved, turned,
                        )
                        return
                    x = (x + x2) / 2.0
                    y = (y + y2) / 2.0
                    # Half the shortest-arc delta -- the parentheses matter:
                    # `%` binds tighter than `+`, so without them this lands on
                    # theta2 instead of between the two.
                    theta = theta + ((theta2 - theta + 180.0) % 360.0 - 180.0) / 2.0
                self._captures.append(
                    (x, y, theta, points, float(payload.get("rotationSpeed") or 0.0))
                )
        except Exception as err:  # noqa: BLE001 -- one bad read must never end a run
            _LOGGER.debug("LIDAR mapping: sample failed (%s)", err)
        finally:
            self._busy = False

        if time.monotonic() - self._last_health >= HEALTH_EVERY:
            self._check_health()

    def _recording_session(self) -> dict[str, Any] | None:
        for item in (self.coordinator.data or {}).get("history") or ():
            if isinstance(item, dict) and item.get("recording"):
                return item
        return None

    def _check_health(self) -> None:
        """Slow sampling down if the robot's own pose logging is thinning out.

        Compares the recording file's growth against the firmware's 2 s
        cadence, averaged over the whole run so the 30 s flush granularity
        cannot fake a collapse. It never stops collection: the measurement is
        too coarse to justify throwing a run away.
        """
        self._last_health = time.monotonic()
        cur = self._recording_session()
        if not cur or not self._health_ref:
            self._health_ref = cur or self._health_ref
            return
        if cur.get("name") != self._health_ref.get("name"):
            # A new file means a new session; re-anchor rather than compare.
            self._health_ref = cur
            self._health_start = time.monotonic()
            return

        elapsed = time.monotonic() - self._health_start
        if elapsed < HEALTH_GRACE:
            return
        expected = elapsed / 2.0 * BYTES_PER_POSE
        if expected <= 0:
            return
        ratio = (cur.get("size", 0) - self._health_ref.get("size", 0)) / expected

        if ratio < BACKOFF_RATIO and self._interval < MAX_INTERVAL:
            self._interval = min(MAX_INTERVAL, self._interval * 1.5)
            _LOGGER.info(
                "LIDAR mapping: robot pose logging at %.0f%% of normal over %.0fs; "
                "sampling every %.0fs (%d captures so far)",
                100 * ratio, elapsed, self._interval, len(self._captures),
            )
            self._start_timer()
        else:
            _LOGGER.debug(
                "LIDAR mapping: pose logging at %.0f%% over %.0fs, %d captures",
                100 * ratio, elapsed, len(self._captures),
            )

    # ── completion ──────────────────────────────────────────────────

    async def _finish(self) -> None:
        self._collecting = False
        self._stop_timer()
        captures, self._captures = self._captures, []
        if len(captures) < MIN_CAPTURES:
            _LOGGER.info(
                "LIDAR mapping: only %d captures, too thin to merge", len(captures)
            )
            return

        captures = self._drop_slow_scans(captures)
        if len(captures) < MIN_CAPTURES:
            _LOGGER.info(
                "LIDAR mapping: only %d captures left after the rotation filter, "
                "too thin to merge", len(captures),
            )
            return

        walls, floor = await self.hass.async_add_executor_job(
            build_session_grids, [c[:4] for c in captures]
        )
        report = await self.hass.async_add_executor_job(
            self._map.merge_session, walls, floor, self._session_name
        )
        self.last_report = report
        if report.get("rejected"):
            return

        await self._store.async_save(self._map.as_dict())
        _LOGGER.info(
            "LIDAR map updated: %d wall cells after %d cleanings",
            report.get("total_walls", 0), report.get("sessions", 0),
        )

    @staticmethod
    def _drop_slow_scans(captures: list) -> list:
        """Discard scans taken while the laser was not sweeping properly.

        Judged against the run's own median rather than a fixed rpm: the
        field's unit is undocumented and the docked reading (5.03) gives no
        usable reference. A run spends most of its time at its normal speed,
        so the median *is* the normal speed, and the outliers below it are the
        spin-ups and stalls whose 360 angles were never swept in one turn.
        """
        speeds = [c[4] for c in captures if len(c) > 4 and c[4] > 0]
        if len(speeds) < 5:
            return captures
        speeds.sort()
        median = speeds[len(speeds) // 2]
        floor_speed = median * MIN_ROTATION_FRACTION
        kept = [c for c in captures if len(c) > 4 and c[4] >= floor_speed]
        if len(kept) < len(captures):
            _LOGGER.info(
                "LIDAR mapping: dropped %d of %d scans below %.1f (median %.1f)",
                len(captures) - len(kept), len(captures), floor_speed, median,
            )
        return kept

    # ── output ──────────────────────────────────────────────────────

    async def async_render(self) -> tuple[bytes, dict[str, float]] | None:
        """Render the accumulated map, or None if nothing is mapped yet."""
        if not self._map or not self._map.walls:
            return None
        return await self.hass.async_add_executor_job(
            render_plan, self._map.walls, self._map.floor
        )

    def calibration(self) -> dict[str, float] | None:
        """Where the plan sits in the world, without rendering it."""
        if not self._map or not self._map.walls:
            return None
        return plan_calibration(self._map.walls, self._map.floor)

    def alignment(self, session_name: str) -> tuple[int, int, int] | None:
        """How this session was corrected onto the map, if it was merged.

        The card needs it to draw the run in the same frame as the walls;
        without it a session recorded after a localisation loss shows its
        cleaned area a quarter turn off.
        """
        if not self._map:
            return None
        return self._map.alignments.get(session_name)

    def view_rotation(self, user_offset: float = 0.0) -> float:
        """Rotation that stands the map upright, plus the user's offset."""
        return self._map.view_rotation(user_offset) if self._map else user_offset

    @property
    def sessions(self) -> int:
        return self._map.sessions if self._map else 0
