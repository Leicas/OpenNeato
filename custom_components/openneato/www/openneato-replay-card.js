/*
 * OpenNeato replay card
 *
 * Plays a cleaning session back on a canvas at display refresh rate, the way
 * the robot's own web UI does. The rendering is a port of
 * frontend/src/views/history/helpers.ts::renderMap and the playback loop is a
 * port of frontend/src/views/history/motion-player.tsx, so what you see here
 * and what you see at http://neato.local/#/history/... are the same picture.
 *
 * Session data arrives pre-parsed over the WebSocket connection
 * (openneato/sessions, openneato/session) — the browser only draws.
 */

const CARD_VERSION = "1.1.0";

// Breathing room around the fitted map, in CSS pixels. Kept small: the fit
// already leaves slack wherever the run is not the shape of the card, and
// padding on top of that is space the map does not get.
const MAP_PAD = 8;
// The lattice: one square per occupancy cell, with a gap this fraction of the
// period. The reference plan works out at a period of 3 px with a 1 px gutter,
// so a third; a quarter reads the same and leaves more colour in the square.
const GRID_GUTTER_RATIO = 0.28;
// Floor for `height: fill`, so a short column cannot squeeze the map to
// nothing.
const FILL_MIN_HEIGHT = 220;
// Cell size to fall back on before a session has loaded. The robot maps at
// 5 cm and so does lidar_mapper.CELL_M.
const DEFAULT_CELL_M = 0.05;
// Cleaned floor. Opaque and flat: a square is cleaned or it is not, and a
// translucent wash would put a fourth colour on the map wherever it overlapped
// something.
const COVERAGE_COLOR = "#a8cddb";
// The robot's recent track: the same hue as the cleaned floor, taken much
// darker, so it reads as depth rather than as a fourth state on the map.
const TRAIL_COLOR = "#0d4d63";
// How far back the track reaches, in seconds of the *session*, so its length
// does not change with playback speed.
const TRAIL_SECONDS = 120;
// Opacity of the permanent trace — every square the robot has crossed, kept
// faint enough to sit under the coverage reading without competing with it.
const TRAIL_PERSIST_ALPHA = 0.3;
const GRID_STEP = 0.5;
// Longest wall-clock step a single playback frame may advance the playhead.
const MAX_FRAME_DT = 1 / 15;
// Playback rates offered by the speed button, in session-seconds per real
// second. 1 is true real time; a full house clean runs close to an hour, so
// the useful range goes well past it.
const SPEED_STEPS = [1, 2, 4, 8, 16, 32, 64];
// World grid the viewport snaps to. One metre swallows the usual half-metre
// of drift in where a cleaning run ends up reaching, without wasting much of
// the canvas on margin.
const FIT_QUANTUM_M = 1;
// The same idea when a floorplan already anchors the frame — see _viewBounds.
const FIT_QUANTUM_ANCHORED_M = 0.25;
// Polling. One timer ticks every POLL_TICK_MS and decides what is due. The
// session list is served from Home Assistant's own coordinator cache, so
// re-reading it costs the robot nothing; a live session download does reach
// the ESP32 (the integration caps it server-side), so that one is slower and
// only runs while the selected session is being recorded.
const POLL_TICK_MS = 5000;
const SESSIONS_REFRESH_MS = 10000;
const LIVE_REFRESH_MS = 15000;
// A live view that has not managed a refresh for this long is flagged stale.
const LIVE_STALE_MS = 60000;
// Same factors as frontend/src/distance-units.ts.
const METERS_TO_FEET = 3.28084;
const SQM_TO_SQFT = 10.7639;
const DEFAULTS = {
    speed: 8,
    // "auto" follows the wall-derived angle a generated plan reports, so the
    // map stays upright even if the robot re-zeroes its frame. A number
    // pins the view instead.
    rotation: "auto",
    autoplay: false,
    floorplan: true,
    floorplan_opacity: 1,
    // The Neato-app look: half-metre lines, the plan quantised onto the
    // lattice, coverage and trail as square tiles. false gives a flat map —
    // no lines, smooth plan, smooth swept area and a rounded track.
    grid: true,
    // Per-layer overrides of `grid`; unset follows it. `tiled_coverage`
    // covers the trail too: it is painted on the same lattice as the
    // coverage so the two never read as different drawings.
    grid_lines: undefined,
    tiled_plan: undefined,
    tiled_coverage: undefined,
    // "auto" follows Home Assistant's unit system; "metric" or "imperial"
    // overrides it.
    units: "auto",
    // Fading track of squares behind the robot. Set false for cleaned/not
    // cleaned only.
    trail: true,
    // "stable"  — frame on the cleaned area snapped to a 1 m world grid, so
    //             the view (and the background under it) stops shifting
    //             between cleanings. Works whether or not a plan is set up.
    // "plan"    — as above, but always keeps the whole floorplan in view.
    //             Only sensible once the plan is calibrated onto the
    //             cleaned area.
    // "session" — raw cleaned-area bounds; tightest, but the view shifts and
    //             rescales from one cleaning to the next.
    fit: "stable",
    show_picker: true,
    show_stats: true,
    height: 360,
    session: "latest",
};

/* ── small helpers ──────────────────────────────────────────────────── */

const pad2 = (n) => String(n).padStart(2, "0");

function formatClock(secs) {
    const total = Math.max(0, Math.floor(secs));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}

function formatDate(epoch) {
    const d = new Date(epoch * 1000);
    return d.toLocaleString(undefined, {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    });
}

function modeLabel(mode) {
    return { house: "House Clean", spot: "Spot Clean", manual: "Manual Clean" }[mode] || mode || "Clean";
}

// "metric" | "imperial" from the card option, else from Home Assistant's
// unit system. `area` is the exact unit the user chose on recent cores;
// `length` ("km" | "mi") is the fallback every version has. Anything else
// in the option is treated as "auto" — a typo must not blank the card.
function resolveUnits(config, hass) {
    const u = config && config.units;
    if (u === "metric" || u === "imperial") return u;
    const us = (hass && hass.config && hass.config.unit_system) || {};
    if (us.area === "ft²" || us.area === "ft^2") return "imperial";
    if (us.area) return "metric";
    return us.length === "mi" ? "imperial" : "metric";
}

function formatNumber(value, locale, digits) {
    try {
        return new Intl.NumberFormat(locale || undefined, {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        }).format(value);
    } catch {
        return value.toFixed(digits);
    }
}

// Metres to one decimal, feet as whole numbers: a foot is three times finer
// and a decimal on it would claim centimetre accuracy the robot does not
// have. Non-breaking space before the unit so a label never wraps there.
function formatArea(m2, units, locale) {
    const n = Number(m2);
    if (m2 === undefined || m2 === null || Number.isNaN(n)) return "";
    return units === "imperial"
        ? `${formatNumber(n * SQM_TO_SQFT, locale, 0)}\u00a0ft²`
        : `${formatNumber(n, locale, 1)}\u00a0m²`;
}

function formatDistance(m, units, locale) {
    const n = Number(m);
    if (m === undefined || m === null || Number.isNaN(n)) return "";
    return units === "imperial"
        ? `${formatNumber(n * METERS_TO_FEET, locale, 0)}\u00a0ft`
        : `${formatNumber(n, locale, 1)}\u00a0m`;
}

// HA websocket errors carry `code` and `message`; match the code and fall
// back to the backend's wording in case a proxy drops it.
function isEmptySessionError(err) {
    if (!err) return false;
    if (err.code === "empty_session") return true;
    return /no pose data/i.test(String(err.message || ""));
}

// Shortest-arc interpolation between two headings in degrees.
function lerpAngleDeg(a, b, f) {
    const delta = ((b - a + 540) % 360) - 180;
    return a + delta * f;
}

/* ── session model over the flat wire arrays ────────────────────────── */

// The server ships path as [x, y, theta, ts, ...] and coverage as
// [cx, cy, ts, ...]. Wrapping them in typed arrays keeps per-frame indexing
// cheap and avoids allocating tens of thousands of objects per session.
class Session {
    constructor(raw) {
        this.name = raw.name;
        this.session = raw.session;
        this.summary = raw.summary;
        this.bounds = raw.bounds;
        this.cellSize = raw.cellSize;
        this.duration = raw.duration || 0;
        this.recharges = raw.recharges || [];
        this.floorplan = raw.floorplan || null;

        this.path = Float64Array.from(raw.path || []);
        this.poseCount = this.path.length / 4;

        // Coverage is revealed in timestamp order during playback, so sort
        // once here and the renderer can append newly-lit cells frame to
        // frame instead of re-testing every cell against the playhead.
        const flat = raw.coverage || [];
        const count = flat.length / 3;
        const order = new Uint32Array(count);
        for (let i = 0; i < count; i++) order[i] = i;
        const tsAt = (i) => flat[i * 3 + 2];
        const sorted = Array.prototype.sort.call(order, (a, b) => tsAt(a) - tsAt(b));
        this.coverage = new Float64Array(count * 3);
        for (let i = 0; i < count; i++) {
            const src = sorted[i] * 3;
            this.coverage[i * 3] = flat[src];
            this.coverage[i * 3 + 1] = flat[src + 1];
            this.coverage[i * 3 + 2] = flat[src + 2];
        }
        this.cellCount = count;
    }

    poseTs(i) {
        return this.path[i * 4 + 3];
    }

    // Robot pose at a session-relative timestamp, linearly interpolated in
    // position and shortest-arc in heading so the sprite never snaps.
    interpolate(ts) {
        const n = this.poseCount;
        if (n === 0) return null;
        if (ts <= this.poseTs(0)) {
            return { x: this.path[0], y: this.path[1], t: this.path[2], ts: this.path[3] };
        }
        const lastBase = (n - 1) * 4;
        if (ts >= this.path[lastBase + 3]) {
            return {
                x: this.path[lastBase],
                y: this.path[lastBase + 1],
                t: this.path[lastBase + 2],
                ts: this.path[lastBase + 3],
            };
        }
        let lo = 0;
        let hi = n - 1;
        while (hi - lo > 1) {
            const mid = (lo + hi) >> 1;
            if (this.poseTs(mid) <= ts) lo = mid;
            else hi = mid;
        }
        const a = lo * 4;
        const b = hi * 4;
        const span = this.path[b + 3] - this.path[a + 3];
        const f = span > 0 ? (ts - this.path[a + 3]) / span : 0;
        return {
            x: this.path[a] + (this.path[b] - this.path[a]) * f,
            y: this.path[a + 1] + (this.path[b + 1] - this.path[a + 1]) * f,
            t: lerpAngleDeg(this.path[a + 2], this.path[b + 2], f),
            ts,
        };
    }

    // Index of the first pose strictly after `ts` — i.e. how many poses of
    // the path line have been drawn by then.
    poseCountUpTo(ts) {
        let lo = 0;
        let hi = this.poseCount;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this.poseTs(mid) <= ts) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }
}

/* ── the card ───────────────────────────────────────────────────────── */

class OpenNeatoReplayCard extends HTMLElement {
    constructor() {
        super();
        this.attachShadow({ mode: "open" });

        this._hass = null;
        this._config = { ...DEFAULTS };
        this._sessions = [];
        this._session = null;
        this._selectedName = null;
        this._loading = false;
        this._sessionsLoading = false;
        // Outcome of the last fetch for `_selectedName`: null while nothing
        // has been tried, then "ok" | "empty" | "error" | "missing" (a pinned
        // session that is not on the robot yet). A failed completed
        // session is not retried by the list tick; a live one is retried on
        // the live cadence.
        this._loadState = null;
        this._error = null;
        this._floorplanImg = null;
        this._floorplanKey = null;

        // Which layers get the lattice treatment; resolved in setConfig.
        this._look = { lines: true, plan: true, tiles: true };
        this._units = "metric";
        this._pickerSig = null;

        // Live session state. `_live`: the selected session is still being
        // recorded. `_follow`: the playhead rides the newest frame. The user
        // pins a manual pick with `_userPicked`; a new clean lifts it.
        this._live = false;
        this._follow = true;
        this._userPicked = false;
        this._seenLive = null;
        this._finalDue = false;
        this._liveLoading = false;
        this._lastListAt = 0;
        this._lastLiveAt = 0;
        this._lastLiveOk = 0;
        this._liveSince = 0;
        this._pollTimer = null;
        this._durTimer = null;
        this._quarterLock = null;
        this._onVisibilityBound = () => this._onVisibility();

        // Playback state. `_time` is the source of truth and is mutated by
        // the rAF loop directly — putting it in a re-render cycle is what
        // makes this kind of player stutter.
        this._time = 0;
        this._speed = DEFAULTS.speed;
        this._playing = false;
        this._raf = null;
        this._lastFrame = 0;
        this._dirty = true;

        // View transform (pan/zoom), applied on top of the fitted projection.
        this._tf = { panX: 0, panY: 0, zoom: 1 };
        this._drag = null;

        // Incrementally-built coverage layer, see _paintCoverage().
        this._cov = { canvas: null, ctx: null, sig: "", cursor: 0, time: -1 };
        // Incrementally-built permanent track, see _paintHistory().
        this._trk = { canvas: null, ctx: null, sig: "", cursor: 0, time: -1, painted: null };
    }

    /* ---- Lovelace plumbing ---- */

    static getStubConfig() {
        return { type: "custom:openneato-replay-card" };
    }

    setConfig(config) {
        this._config = { ...DEFAULTS, ...config };
        this._speed = Number(this._config.speed) > 0 ? Number(this._config.speed) : DEFAULTS.speed;
        this._selectedName =
            this._config.session && this._config.session !== "latest" ? this._config.session : null;
        const master = this._config.grid ?? DEFAULTS.grid;
        this._look = {
            lines: this._config.grid_lines ?? master,
            plan: this._config.tiled_plan ?? master,
            tiles: this._config.tiled_coverage ?? master,
        };
        this._buildDom();
    }

    // Make the wrapper Home Assistant puts around this card stretch.
    //
    // `height: fill` is useless on its own: the card sits inside a `hui-card`
    // that sizes to its content, so asking for 100% of an auto height gives
    // back the content height. Styling that wrapper from the dashboard does
    // not work either — `stack-in-card` renders its children through a
    // `hui-vertical-stack-card`, which puts the wrapper a second shadow root
    // down, and CSS does not cross shadow boundaries. Reaching one level out
    // from here is the only way left, so keep it to exactly that: set flex on
    // the immediate parent and touch nothing else.
    _stretchWrapper() {
        const wrapper = this.parentElement;
        if (!wrapper) return;
        wrapper.style.flex = "1 1 auto";
        wrapper.style.minHeight = "0";
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
    }

    getCardSize() {
        const h = Number(this._config.height);
        return Math.ceil((Number.isFinite(h) ? h : DEFAULTS.height) / 50) + 1;
    }

    // Home Assistant calls this on every state change. It only stores the
    // object and re-reads the unit system; polling is on a timer of its own.
    set hass(hass) {
        const first = this._hass === null;
        this._hass = hass;
        const units = resolveUnits(this._config, hass);
        if (units !== this._units) {
            this._units = units;
            this._renderPicker();
            this._renderStats();
        }
        if (first) this._loadSessions();
        // connectedCallback starts the timer for a card in the tree; an
        // editor preview that never gets attached must not poll forever.
        if (this.isConnected) this._ensurePoll();
    }

    connectedCallback() {
        // setConfig runs before the card is in the tree, so the wrapper may not
        // have existed yet; do it again now that it certainly does.
        if (this._config && this._config.height === "fill") this._stretchWrapper();
        if (this._canvas) this._observeResize();
        document.addEventListener("visibilitychange", this._onVisibilityBound);
        this._ensurePoll();
        if (this._live) this._startDurationTicker();
        this._dirty = true;
        this._scheduleRender();
    }

    disconnectedCallback() {
        this._stopLoop();
        this._stopPoll();
        document.removeEventListener("visibilitychange", this._onVisibilityBound);
        if (this._resizeObserver) this._resizeObserver.disconnect();
    }

    /* ---- DOM ---- */

    _buildDom() {
        if (this._built) return;
        this._built = true;

        const root = this.shadowRoot;
        root.innerHTML = `
            <style>
                ha-card {
                    display: flex;
                    flex-direction: column;
                    overflow: hidden;
                }
                /* Several elements below set their own display, which would
                   otherwise beat the hidden attribute. */
                [hidden] {
                    display: none !important;
                }
                /* One row: title (only if configured), then the session
                   picker, then the stats. The stats scroll inside their own
                   box rather than wrapping, so the header stays one line at
                   any width. */
                .head {
                    display: flex;
                    align-items: center;
                    gap: 12px;
                    padding: 12px 16px 8px;
                    flex-wrap: nowrap;
                }
                .title {
                    font-size: 1.1rem;
                    font-weight: 500;
                    color: var(--primary-text-color);
                    flex: 0 1 auto;
                    min-width: 0;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                /* An empty title is the common config — collapse it so the
                   picker really is the first thing on the row. */
                .title:empty {
                    display: none;
                }
                select {
                    background: var(--secondary-background-color);
                    color: var(--primary-text-color);
                    border: 1px solid var(--divider-color);
                    border-radius: 6px;
                    padding: 4px 8px;
                    font-size: 0.85rem;
                    flex: 0 1 auto;
                    min-width: 0;
                    max-width: 55%;
                }
                .stats {
                    display: flex;
                    flex-wrap: nowrap;
                    gap: 16px;
                    font-size: 0.8rem;
                    color: var(--secondary-text-color);
                    flex: 1 1 auto;
                    min-width: 0;
                    overflow-x: auto;
                    scrollbar-width: none;
                }
                .stats::-webkit-scrollbar {
                    display: none;
                }
                .stats span {
                    white-space: nowrap;
                }
                .del {
                    flex: 0 0 auto;
                    display: grid;
                    place-items: center;
                    width: 28px;
                    height: 28px;
                    padding: 0;
                    border: none;
                    border-radius: 6px;
                    background: transparent;
                    color: var(--secondary-text-color);
                    cursor: pointer;
                }
                .del svg {
                    width: 17px;
                    height: 17px;
                    fill: currentColor;
                }
                .del:hover:not(:disabled) {
                    color: var(--error-color, #db4437);
                    background: var(--secondary-background-color);
                }
                .del:disabled {
                    opacity: 0.35;
                    cursor: default;
                }
                .stats b {
                    color: var(--primary-text-color);
                    font-weight: 500;
                }
                .stage {
                    position: relative;
                    width: 100%;
                    background: var(--card-background-color);
                }
                canvas {
                    /* Absolute, so the canvas never contributes to layout. Its
                       width/height attributes are device pixels and act as an
                       intrinsic size; left in the flow inside a flexible stage
                       they feed the stage's height, which resizes the canvas,
                       which grows the stage again — the box ran away to ten
                       thousand pixels before this. */
                    position: absolute;
                    inset: 0;
                    display: block;
                    width: 100%;
                    height: 100%;
                    touch-action: none;
                    cursor: grab;
                }
                canvas.dragging { cursor: grabbing; }
                .overlay {
                    position: absolute;
                    inset: 0;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    gap: 4px;
                    color: var(--primary-text-color);
                    font-size: 0.9rem;
                    text-align: center;
                    padding: 16px;
                    white-space: pre-line;
                    pointer-events: none;
                }
                /* Second line: what to do about it. */
                .overlay .hint {
                    color: var(--secondary-text-color);
                    font-size: 0.8rem;
                }
                .controls {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    padding: 8px 16px 12px;
                }
                button {
                    background: none;
                    border: none;
                    padding: 6px;
                    border-radius: 50%;
                    color: var(--primary-text-color);
                    cursor: pointer;
                    display: inline-flex;
                    line-height: 0;
                }
                button:hover { background: var(--secondary-background-color); }
                button:disabled { opacity: 0.4; cursor: default; }
                svg { width: 20px; height: 20px; fill: currentColor; }
                button.speed {
                    border-radius: 10px;
                    padding: 3px 8px;
                    font: inherit;
                    font-size: 0.75rem;
                    font-variant-numeric: tabular-nums;
                    line-height: 1.4;
                    color: var(--secondary-text-color);
                    border: 1px solid var(--divider-color);
                    min-width: 38px;
                }
                /* Live chip: filled while following the newest frame, outlined
                   as a "Go live" button once the user has scrubbed back. */
                button.live {
                    /* The theme's error red is only ~3.9:1 against white;
                       darkened a fifth it clears 4.5:1 for the filled chip
                       and the outlined ink alike. */
                    --live: var(--error-color, #db4437);
                    --live: color-mix(in srgb, var(--error-color, #db4437) 80%, #000);
                    border-radius: 10px;
                    padding: 3px 8px;
                    font: inherit;
                    font-size: 0.75rem;
                    line-height: 1.4;
                    white-space: nowrap;
                    color: var(--live);
                    border: 1px solid var(--live);
                }
                button.live.on {
                    background: var(--live);
                    color: #fff;
                    cursor: default;
                }
                button.live.on:hover {
                    background: var(--live);
                }
                button.live .dot {
                    margin-right: 4px;
                    animation: live-pulse 2s ease-in-out infinite;
                }
                button.live.stale {
                    opacity: 0.6;
                }
                button.live.stale .dot {
                    animation: none;
                }
                @keyframes live-pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.4; }
                }
                @media (prefers-reduced-motion: reduce) {
                    button.live .dot { animation: none; }
                }
                .clock {
                    font-variant-numeric: tabular-nums;
                    font-size: 0.8rem;
                    color: var(--secondary-text-color);
                    min-width: 42px;
                    text-align: center;
                }
                input[type="range"] {
                    flex: 1;
                    -webkit-appearance: none;
                    appearance: none;
                    height: 6px;
                    border-radius: 3px;
                    background:
                        linear-gradient(to right,
                            rgba(0, 0, 0, 0) var(--played, 0%),
                            var(--unplayed-veil) var(--played, 0%)),
                        var(--track, var(--divider-color));
                    outline: none;
                    cursor: pointer;
                    --unplayed-veil: rgba(120, 120, 120, 0.55);
                }
                input[type="range"]::-webkit-slider-thumb {
                    -webkit-appearance: none;
                    width: 14px;
                    height: 14px;
                    border-radius: 50%;
                    background: var(--primary-text-color);
                    border: 2px solid var(--card-background-color);
                }
                input[type="range"]::-moz-range-thumb {
                    width: 14px;
                    height: 14px;
                    border-radius: 50%;
                    background: var(--primary-text-color);
                    border: 2px solid var(--card-background-color);
                }
            </style>
            <ha-card>
                <div class="head">
                    <div class="title">Cleaning replay</div>
                    <select class="picker" aria-label="Cleaning session"></select>
                    <button class="del" title="Delete this session" aria-label="Delete this session" disabled>
                        <svg viewBox="0 0 24 24"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                    </button>
                    <div class="stats"></div>
                </div>
                <div class="stage">
                    <canvas></canvas>
                    <div class="overlay">Loading…</div>
                </div>
                <div class="controls">
                    <button class="play" title="Play/Pause" disabled>
                        <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                    </button>
                    <button class="restart" title="Restart" disabled>
                        <svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/></svg>
                    </button>
                    <button class="speed" title="Playback speed" disabled>8&times;</button>
                    <span class="clock elapsed">0:00</span>
                    <input type="range" class="scrub" min="0" max="1" step="0.05" value="0" disabled>
                    <span class="clock total">0:00</span>
                    <button class="live" hidden></button>
                </div>
            </ha-card>
        `;

        this._card = root.querySelector("ha-card");
        this._head = root.querySelector(".head");
        this._picker = root.querySelector(".picker");
        this._statsEl = root.querySelector(".stats");
        this._delBtn = root.querySelector(".del");
        this._stage = root.querySelector(".stage");
        this._canvas = root.querySelector("canvas");
        this._overlay = root.querySelector(".overlay");
        this._playBtn = root.querySelector(".play");
        this._restartBtn = root.querySelector(".restart");
        this._speedBtn = root.querySelector(".speed");
        this._elapsedEl = root.querySelector(".elapsed");
        this._totalEl = root.querySelector(".total");
        this._scrub = root.querySelector(".scrub");
        this._liveBtn = root.querySelector(".live");

        // `height: fill` lets the map grow to whatever room the column gives
        // it, instead of pinning a pixel count that has to be re-guessed every
        // time a card next to it gains a row. The canvas already watches its
        // own box through a ResizeObserver, so growing the stage is all that
        // is needed for the map to re-fit itself.
        if (this._config.height === "fill") {
            this.style.height = "100%";
            this._card.style.height = "100%";
            this._stage.style.flex = "1 1 0";
            this._stage.style.minHeight = `${FILL_MIN_HEIGHT}px`;
            this._stretchWrapper();
        } else {
            this._stage.style.height = `${Number(this._config.height) || DEFAULTS.height}px`;
        }
        this._picker.hidden = !this._config.show_picker;
        // The delete button acts on the picker's selection, so it follows it.
        // Set `show_picker: false` to hide both.
        this._delBtn.hidden = !this._config.show_picker;
        this._statsEl.hidden = !this._config.show_stats;
        if (this._config.title !== undefined) {
            root.querySelector(".title").textContent = this._config.title;
        }

        this._picker.addEventListener("change", () => {
            // A manual pick is pinned: list refreshes must not pull the user
            // off it onto a clean that is already running.
            this._userPicked = true;
            this._selectSession(this._picker.value);
        });
        this._delBtn.addEventListener("click", () => this._deleteSelected());
        this._playBtn.addEventListener("click", () => this._togglePlay());
        this._restartBtn.addEventListener("click", () => this._restart());
        this._speedBtn.addEventListener("click", () => this._cycleSpeed());
        this._liveBtn.addEventListener("click", () => {
            if (!this._follow) this._goLive();
        });
        this._showSpeed();
        // Grabbing the thumb already means "stop following", before any
        // value has changed — otherwise a refresh could yank it away.
        this._scrub.addEventListener("pointerdown", () => {
            this._follow = false;
            this._renderChip();
        });
        this._scrub.addEventListener("input", () => {
            this._pause();
            this._follow = false;
            this._seek(Number(this._scrub.value));
            this._renderChip();
        });

        this._bindGestures();
        this._observeResize();
    }

    _observeResize() {
        if (this._resizeObserver) this._resizeObserver.disconnect();
        this._resizeObserver = new ResizeObserver(() => {
            this._dirty = true;
            this._scheduleRender();
        });
        this._resizeObserver.observe(this._stage);
    }

    // Pan with drag, zoom with wheel/pinch, anchored on the pointer so the
    // point under the cursor stays put — same feel as the robot's web UI.
    _bindGestures() {
        const canvas = this._canvas;

        canvas.addEventListener("pointerdown", (e) => {
            canvas.setPointerCapture(e.pointerId);
            this._drag = { x: e.clientX, y: e.clientY, panX: this._tf.panX, panY: this._tf.panY };
            canvas.classList.add("dragging");
        });
        canvas.addEventListener("pointermove", (e) => {
            if (!this._drag) return;
            // The pan is applied inside the rotation, so a screen-space drag
            // has to be turned back into that frame first. Miss this and the
            // map slides off at whatever angle the view is straightened by —
            // with the quarter turn in play, dragging up sent it right.
            const d = this._unrotate(e.clientX - this._drag.x, e.clientY - this._drag.y);
            this._tf.panX = this._drag.panX + d.x;
            this._tf.panY = this._drag.panY + d.y;
            this._dirty = true;
            this._scheduleRender();
        });
        const endDrag = (e) => {
            if (!this._drag) return;
            this._drag = null;
            canvas.classList.remove("dragging");
            if (e && e.pointerId !== undefined && canvas.hasPointerCapture(e.pointerId)) {
                canvas.releasePointerCapture(e.pointerId);
            }
        };
        canvas.addEventListener("pointerup", endDrag);
        canvas.addEventListener("pointercancel", endDrag);

        canvas.addEventListener(
            "wheel",
            (e) => {
                e.preventDefault();
                const rect = canvas.getBoundingClientRect();
                const w = rect.width;
                const h = rect.height;
                // The cursor, carried back through the rotation, for the same
                // reason the drag is: the anchor has to be expressed in the
                // frame the pan lives in.
                const u = this._unrotate(e.clientX - rect.left - w / 2, e.clientY - rect.top - h / 2);
                const cx = u.x + w / 2;
                const cy = u.y + h / 2;
                const factor = Math.exp(-e.deltaY * 0.0015);
                const next = Math.min(8, Math.max(1, this._tf.zoom * factor));
                const applied = next / this._tf.zoom;
                // Keep the world point under the cursor fixed across the zoom.
                this._tf.panX = cx - (cx - this._tf.panX) * applied;
                this._tf.panY = cy - (cy - this._tf.panY) * applied;
                this._tf.zoom = next;
                if (next === 1) {
                    this._tf.panX = 0;
                    this._tf.panY = 0;
                }
                this._dirty = true;
                this._scheduleRender();
            },
            { passive: false },
        );

        canvas.addEventListener("dblclick", () => {
            this._tf = { panX: 0, panY: 0, zoom: 1 };
            this._dirty = true;
            this._scheduleRender();
        });
    }

    /* ---- polling ---- */

    _ensurePoll() {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => this._tick(), POLL_TICK_MS);
    }

    _stopPoll() {
        if (this._pollTimer) clearInterval(this._pollTimer);
        this._pollTimer = null;
        this._stopDurationTicker();
    }

    // Both polls pause while the dashboard tab is hidden; _onVisibility
    // catches up the moment it is shown again.
    _tick() {
        if (!this._hass || document.hidden) return;
        const now = Date.now();
        if (now - this._lastListAt >= SESSIONS_REFRESH_MS) this._loadSessions();
        const wantsLive = this._live || this._finalDue;
        const due = this._finalDue || now - this._lastLiveAt >= LIVE_REFRESH_MS;
        if (wantsLive && due && this._selectedName && !this._loading && !this._liveLoading) {
            this._refreshLive();
        }
        if (this._live) this._renderChip(); // picks up the stale state
    }

    _onVisibility() {
        if (document.hidden) return;
        this._lastListAt = 0;
        this._lastLiveAt = 0;
        this._tick();
    }

    // The live Duration stat counts up between refreshes so it does not
    // stall for fifteen seconds at a time.
    _startDurationTicker() {
        if (this._durTimer) return;
        this._durTimer = setInterval(() => {
            if (this._live && !document.hidden) this._renderStats();
        }, 1000);
    }

    _stopDurationTicker() {
        if (this._durTimer) clearInterval(this._durTimer);
        this._durTimer = null;
    }

    /* ---- data loading ---- */

    _locale() {
        const hass = this._hass;
        return (hass && hass.locale && hass.locale.language) || navigator.language;
    }

    async _loadSessions() {
        if (this._sessionsLoading || !this._hass) return;
        this._sessionsLoading = true;
        this._lastListAt = Date.now();
        try {
            const res = await this._hass.callWS({
                type: "openneato/sessions",
                ...(this._config.entry_id ? { entry_id: this._config.entry_id } : {}),
            });
            this._entryId = res.entry_id;
            this._sessions = res.sessions || [];
            this._renderPicker();
            if (this._sessions.length === 0) {
                this._clearSelection();
                this._fail("No cleaning sessions yet\nStart a clean and it will show up here.");
                return;
            }
            await this._reconcileSelection();
        } catch (err) {
            if (this._session) {
                // Whatever is on screen stays; the next tick tries again.
                console.warn("openneato-replay-card: session list unavailable", err);
            } else {
                this._fail(`Could not list sessions\n${err.message || err}`);
            }
        } finally {
            this._sessionsLoading = false;
        }
    }

    _clearSelection() {
        this._selectedName = null;
        this._session = null;
        this._loadState = null;
        this._finalDue = false;
        this._setLive(false);
    }

    // Decide what the card shows after a list refresh, and notice the
    // selected session starting or finishing its recording.
    async _reconcileSelection() {
        const listed = (name) => Boolean(name) && this._sessions.some((s) => s.name === name);
        const live = this._sessions.find((s) => s.recording);
        const liveName = live ? live.name : null;
        // A *new* clean lifts a manual pick. `session: latest` promises to
        // switch to a clean as it starts; the pin only ever meant "leave me
        // on what I chose while this one runs". Caveat: a list that briefly
        // drops the recording entry and gets it back would lift the pin
        // spuriously; rare, since the coordinator carries last-known history
        // forward instead of serving a gap.
        if (liveName && liveName !== this._seenLive) this._userPicked = false;
        this._seenLive = liveName;

        const pinned = this._config.session && this._config.session !== "latest";
        if (pinned && !listed(this._config.session)) {
            // Say so rather than quietly showing some other session; the
            // list keeps polling, so the card picks it up when the file
            // appears.
            if (this._loadState !== "missing") {
                this._clearSelection();
                this._selectedName = this._config.session;
                this._loadState = "missing";
                this._fail(`Session not found\n${this._config.session} is not on the robot.`);
            }
            this._syncDelete();
            return;
        }
        let wanted;
        if (pinned) {
            wanted = this._config.session;
        } else if (liveName && !this._userPicked) {
            wanted = liveName;
        } else if (listed(this._selectedName)) {
            wanted = this._selectedName;
        } else {
            wanted = this._sessions[0].name;
        }

        const nowRecording = Boolean(this._sessions.find((s) => s.name === wanted)?.recording);
        const untried = this._loadState === null || this._loadState === "missing";
        if (wanted !== this._selectedName || (!this._session && untried)) {
            await this._selectSession(wanted);
        } else if (this._live && !nowRecording) {
            // Finished. One more fetch brings the summary — quietly, so the
            // map, pan/zoom and playhead stay where they are — and live mode
            // ends: the chip goes, delete comes back, polling stops.
            this._finalDue = true;
            this._setLive(false);
            await this._refreshLive();
        } else if (!this._live && nowRecording && this._loadState !== null) {
            this._setLive(true);
        }
        this._syncDelete();
    }

    async _deleteSelected() {
        const name = this._selectedName;
        if (!name || this._loading || this._live) return;

        // Label the confirmation with what the user actually sees in the
        // picker, not the raw filename.
        const opt = this._picker.selectedOptions[0];
        const label = opt ? opt.textContent : name;
        if (!window.confirm(`Delete this cleaning session?\n\n${label}\n\nThis cannot be undone.`)) {
            return;
        }

        this._delBtn.disabled = true;
        try {
            await this._hass.callWS({
                type: "openneato/delete_session",
                name,
                ...(this._entryId ? { entry_id: this._entryId } : {}),
            });
        } catch (err) {
            if (err && err.code === "recording") {
                // It started recording between two list ticks. Refresh the
                // list now; the button follows the live flag from there.
                this._flash("Still recording — try again when the clean finishes");
                this._lastListAt = 0;
                this._loadSessions();
            } else {
                this._flash(`Could not delete: ${err.message || err}`);
                this._syncDelete();
            }
            return;
        }

        // Fall back to whichever session is newest once this one is gone.
        this._clearSelection();
        this._lastListAt = 0;
        await this._loadSessions();
    }

    _sessionLabel(s) {
        const start = (s.session && s.session.time) || Number(String(s.name).split(".")[0]);
        const area = s.summary && s.summary.areaCovered;
        let label = `${formatDate(start)} — ${modeLabel(s.session && s.session.mode)}`;
        // No area while recording: the summary is written when the clean
        // ends, and the word says what the picker cannot style.
        if (s.recording) label += " · Live";
        else if (area) label += ` · ${formatArea(area, this._units, this._locale())}`;
        return label;
    }

    // Rebuilt only when the labels change. Rewriting the options on every
    // refresh closed the dropdown under the user and, for a moment, pointed
    // the delete confirmation at the wrong session.
    _renderPicker() {
        if (!this._picker) return;
        const rows = this._sessions.map((s) => [s.name, this._sessionLabel(s)]);
        const sig = `${rows.map(([name, label]) => `${name}|${label}`).join("\n")}|${this._units}`;
        if (sig === this._pickerSig) return;
        this._pickerSig = sig;
        this._picker.innerHTML = rows.map(([name, label]) => `<option value="${name}">${label}</option>`).join("");
        if (this._selectedName) this._picker.value = this._selectedName;
    }

    _fetchSession(name) {
        return this._hass.callWS({
            type: "openneato/session",
            name,
            ...(this._entryId ? { entry_id: this._entryId } : {}),
        });
    }

    // A user-initiated (or first) selection: the one path that clears the
    // view and shows "Loading session…".
    async _selectSession(name) {
        if (!name || this._loading) return;
        this._selectedName = name;
        this._picker.value = name;
        this._loading = true;
        this._loadState = null;
        this._finalDue = false;
        this._pause();
        this._session = null;
        this._follow = true;
        this._tf = { panX: 0, panY: 0, zoom: 1 };
        this._setLive(Boolean(this._sessions.find((s) => s.name === name)?.recording));
        this._enableControls(false);
        this._setOverlay("Loading session…");

        try {
            const raw = await this._fetchSession(name);
            if (name !== this._selectedName) return;
            await this._applySession(raw, name, true);
        } catch (err) {
            if (name === this._selectedName) this._sessionError(err);
        } finally {
            this._loading = false;
        }
    }

    // Quiet re-fetch of the selected session: swaps the data under the view
    // without touching pan/zoom, the playhead or the overlay. Used on the
    // live cadence and for the one fetch after a recording completes.
    async _refreshLive() {
        if (this._liveLoading || this._loading || !this._hass || !this._selectedName) return;
        this._liveLoading = true;
        this._lastLiveAt = Date.now();
        const name = this._selectedName;
        // Only the attempt that was started *as* the completion fetch may
        // retire the flag: if one is already in flight when the recording
        // ends, the early return above leaves `_finalDue` set and the next
        // tick performs it.
        const wasFinal = this._finalDue;
        try {
            const raw = await this._fetchSession(name);
            if (name !== this._selectedName) return;
            // First data for a session that was waiting on it is a first
            // load: frame it, rest on the newest frame.
            await this._applySession(raw, name, !this._session);
        } catch (err) {
            if (name !== this._selectedName) return;
            if (this._session) {
                // Keep what is on screen; the chip turns stale after a minute.
                console.warn("openneato-replay-card: live refresh failed", err);
            } else {
                this._sessionError(err);
            }
        } finally {
            if (wasFinal) this._finalDue = false;
            this._liveLoading = false;
        }
    }

    async _applySession(raw, name, reset) {
        const session = new Session(raw);
        // Same plan URL short-circuits; a changed plan loads before the swap
        // so the old picture stays up until everything new is in hand.
        await this._loadFloorplan(raw.floorplan);
        // The selection may have moved on while the plan loaded.
        if (name !== this._selectedName) return;
        this._session = session;
        this._loadState = "ok";
        this._lastLiveOk = Date.now();
        this._liveSince = this._lastLiveOk;
        // The session payload says whether it is still recording when the
        // backend is new enough; otherwise the list's flag stands.
        if (typeof raw.recording === "boolean" && !this._finalDue) this._setLive(raw.recording);
        // Layers rebuild from the new arrays on the next frame. The view
        // transform is left alone, so pan and zoom survive a refresh.
        this._cov.sig = "";
        this._trk.sig = "";
        if (reset) {
            this._tf = { panX: 0, panY: 0, zoom: 1 };
            this._quarterLock = null;
            this._follow = true;
        }
        this._scrub.max = String(session.duration);
        this._totalEl.textContent = formatClock(session.duration);
        this._enableControls(true);
        this._setOverlay(null);
        if (reset) {
            // Rest on the completed map, like the web player: pressing play
            // rewinds and replays from the beginning. A live session rests on
            // its newest frame, which is the same place.
            this._seek(session.duration);
            if (this._config.autoplay && !this._live) this._restart(true);
        } else if (this._follow) {
            this._seek(session.duration);
        } else {
            // The user scrubbed back: leave the playhead where they put it.
            this._seek(Math.min(this._time, session.duration));
        }
        this._syncDelete();
        this._renderStats();
        this._renderChip();
        this._dirty = true;
        this._scheduleRender();
    }

    _sessionError(err) {
        const msg = err && err.message ? err.message : String(err);
        if (isEmptySessionError(err)) {
            this._loadState = "empty";
            if (this._live) {
                // The file exists but holds no pose yet. Wait for the first
                // one on the live cadence, with no "Loading…" in between.
                this._fail("Cleaning started — waiting for the first map data…");
            } else {
                this._fail("This session has no map data\nThe robot recorded no positions during this clean.");
            }
        } else {
            this._loadState = "error";
            this._fail(`Could not load this session\n${msg}`);
        }
        this._syncDelete();
        this._renderChip();
    }

    async _loadFloorplan(fp) {
        if (!this._config.floorplan || !fp || !fp.url) {
            this._floorplan = null;
            this._floorplanImg = null;
            this._floorplanBounds = null;
            return;
        }
        this._floorplan = fp;
        if (this._floorplanKey === fp.url && this._floorplanImg) return;
        try {
            // <img> can't send an auth header, so ask HA for a signed URL.
            const signed = await this._hass.callWS({
                type: "auth/sign_path",
                path: fp.url,
                expires: 3600,
            });
            const img = new Image();
            await new Promise((resolve, reject) => {
                img.onload = resolve;
                img.onerror = () => reject(new Error("floorplan image failed to load"));
                img.src = signed.path;
            });
            this._floorplanImg = img;
            this._floorplanKey = fp.url;
            this._floorplanBounds = this._planContentBounds(img, fp);
        } catch (err) {
            // A missing plan is cosmetic — fall back to the plain background.
            console.warn("openneato-replay-card: floorplan unavailable", err);
            this._floorplanImg = null;
        }
    }

    _renderStats() {
        if (!this._statsEl || !this._config.show_stats || !this._session) return;
        const s = this._session.summary || {};
        const info = this._session.session || {};
        const units = this._units;
        const locale = this._locale();
        const bits = [];
        // Date, mode and area are all in the picker's own label
        // ("19/08 11:32 — House Clean · 27.3 m²"), so repeating them beside
        // it just spends the row saying the same thing twice. They come back
        // when the picker is hidden and nothing else would show them.
        if (!this._config.show_picker) {
            if (info.time) bits.push(`<span><b>${formatDate(info.time)}</b></span>`);
            bits.push(`<span>${modeLabel(info.mode)}</span>`);
            if (!this._live && s.areaCovered) {
                bits.push(`<span>Area <b>${formatArea(s.areaCovered, units, locale)}</b></span>`);
            }
        }
        if (this._live) {
            // Only what is known while recording — the summary (area,
            // distance, end battery) is written when the clean ends.
            const since = (Date.now() - this._liveSince) / 1000;
            bits.push(`<span>Duration <b>${formatClock(this._session.duration + since)}</b></span>`);
            if (info.battery !== undefined) bits.push(`<span>Battery <b>${info.battery}%</b></span>`);
            const recharges = this._session.recharges.length;
            if (recharges > 0) bits.push(`<span>Recharges <b>${recharges}</b></span>`);
        } else {
            if (s.distanceTraveled) {
                bits.push(`<span>Distance <b>${formatDistance(s.distanceTraveled, units, locale)}</b></span>`);
            }
            const duration = s.duration || this._session.duration;
            if (duration) bits.push(`<span>Duration <b>${formatClock(duration)}</b></span>`);
            if (s.batteryStart !== undefined && s.batteryEnd !== undefined) {
                bits.push(`<span>Battery <b>${s.batteryStart}% → ${s.batteryEnd}%</b></span>`);
            }
            if (s.recharges) bits.push(`<span>Recharges <b>${s.recharges}</b></span>`);
        }
        this._statsEl.innerHTML = bits.join("");
    }

    /* ---- live state ---- */

    _setLive(on) {
        if (this._live !== on) {
            this._live = on;
            if (on) {
                this._lastLiveOk = Date.now();
                this._startDurationTicker();
            } else {
                this._stopDurationTicker();
                // Growing bounds froze the quarter turn; decide it again,
                // once, on the finished map.
                this._quarterLock = null;
            }
        }
        this._syncDelete();
        this._renderChip();
    }

    _syncDelete() {
        if (!this._delBtn) return;
        this._delBtn.disabled = !this._selectedName || this._live;
        const title = this._live ? "Can't delete a session that is still recording" : "Delete this session";
        this._delBtn.title = title;
        this._delBtn.setAttribute("aria-label", title);
    }

    // The one live indicator, in the controls row. Shown only while the
    // selected session is recording *and* something is on screen.
    _renderChip() {
        const chip = this._liveBtn;
        if (!chip) return;
        const show = this._live && Boolean(this._session);
        chip.hidden = !show;
        if (!show) {
            this._chipKey = null;
            return;
        }
        const quiet = Date.now() - this._lastLiveOk;
        const stale = quiet >= LIVE_STALE_MS;
        const key = this._follow ? (stale ? "stale" : "on") : "off";
        if (key !== this._chipKey) {
            // Only rewrite the markup on a state change, or the pulse would
            // restart on every tick.
            this._chipKey = key;
            if (key === "off") {
                chip.className = "live";
                chip.textContent = "Go live";
                chip.removeAttribute("aria-disabled");
            } else {
                chip.className = stale ? "live on stale" : "live on";
                chip.innerHTML = `<span class="dot" aria-hidden="true">●</span>${stale ? "Live · stale" : "Live"}`;
                chip.setAttribute("aria-disabled", "true");
            }
        }
        if (key === "off") chip.title = "Jump to the newest map data";
        else if (stale) chip.title = `No update for ${Math.round(quiet / 1000)} s`;
        else chip.title = "Showing the newest map data";
    }

    _goLive() {
        if (!this._session) return;
        this._pause();
        this._follow = true;
        this._seek(this._session.duration);
        this._renderChip();
    }

    /* ---- overlay ---- */

    // First line is the state; an optional second line (after "\n") says
    // what to do about it.
    _setOverlay(text) {
        this._overlayText = text || null;
        this._overlay.hidden = !text;
        this._overlay.textContent = "";
        if (!text) return;
        const [head, ...rest] = String(text).split("\n");
        const line = document.createElement("div");
        line.textContent = head;
        this._overlay.appendChild(line);
        if (rest.length > 0) {
            const hint = document.createElement("div");
            hint.className = "hint";
            hint.textContent = rest.join("\n");
            this._overlay.appendChild(hint);
        }
    }

    // A transient message over a map that stays on screen; clears itself
    // unless something else has replaced it meanwhile.
    _flash(text) {
        this._setOverlay(text);
        clearTimeout(this._flashTimer);
        this._flashTimer = setTimeout(() => {
            if (this._session && this._overlayText === text) this._setOverlay(null);
        }, 4000);
    }

    _fail(message) {
        this._error = message;
        this._setOverlay(message);
        this._enableControls(false);
    }

    _enableControls(enabled) {
        this._playBtn.disabled = !enabled;
        this._restartBtn.disabled = !enabled;
        this._speedBtn.disabled = !enabled;
        this._scrub.disabled = !enabled;
    }

    /* ---- playback ---- */

    _togglePlay() {
        if (!this._session) return;
        if (this._playing) {
            this._pause();
            return;
        }
        // Playing from the resting end state rewinds first, so the button
        // always does the obvious thing. On a live session that means
        // leaving the newest frame behind for the replay.
        if (this._time >= this._session.duration) this._seek(0);
        this._follow = false;
        this._renderChip();
        this._play();
    }

    _play() {
        if (this._playing || !this._session) return;
        this._playing = true;
        this._setPlayIcon(true);
        this._lastFrame = performance.now();
        this._startLoop();
    }

    _pause() {
        if (!this._playing) return;
        this._playing = false;
        this._setPlayIcon(false);
    }

    // Step to the next rate, wrapping around. A configured speed that isn't
    // one of the presets still works — the cycle picks up from the nearest
    // step above it.
    _cycleSpeed() {
        const next = SPEED_STEPS.find((s) => s > this._speed);
        this._speed = next === undefined ? SPEED_STEPS[0] : next;
        this._showSpeed();
    }

    _showSpeed() {
        if (!this._speedBtn) return;
        // Trim the trailing ".0" that whole rates would otherwise carry.
        const label = Number.isInteger(this._speed) ? this._speed : this._speed.toFixed(1);
        this._speedBtn.textContent = `${label}×`;
        this._speedBtn.title = `Playback speed — ${label}x real time (click to change)`;
    }

    _restart(autoplay = false) {
        this._follow = false;
        this._renderChip();
        this._seek(0);
        if (autoplay) this._play();
        else this._pause();
    }

    _setPlayIcon(playing) {
        this._playBtn.innerHTML = playing
            ? '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>'
            : '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
        this._playBtn.title = playing ? "Pause" : "Play";
    }

    _seek(t) {
        const duration = this._session ? this._session.duration : 0;
        this._time = Math.max(0, Math.min(duration, t));
        this._dirty = true;
        this._scheduleRender();
    }

    _startLoop() {
        if (this._raf !== null) return;
        const tick = (now) => {
            this._raf = null;
            if (this._playing && this._session) {
                // Browsers stop firing rAF for hidden tabs, so `now` can jump
                // by minutes when the dashboard comes back into view. Cap the
                // step at ~4 frames so the playhead resumes instead of
                // teleporting to the end.
                const dt = Math.max(0, Math.min((now - this._lastFrame) / 1000, MAX_FRAME_DT));
                this._lastFrame = now;
                const next = this._time + dt * this._speed;
                if (next < this._session.duration) {
                    this._time = next;
                    this._dirty = true;
                } else if (this._live) {
                    // Caught up with the recording: rest on the newest frame
                    // and follow it from here — each refresh moves the
                    // playhead on, so there is nothing left to play.
                    this._time = this._session.duration;
                    this._follow = true;
                    this._pause();
                    this._renderChip();
                    this._dirty = true;
                } else {
                    this._time = this._session.duration;
                    this._pause();
                    this._dirty = true;
                }
            }
            if (this._dirty) {
                this._dirty = false;
                this._render();
            }
            if (this._playing || this._dirty) this._startLoop();
        };
        this._raf = requestAnimationFrame(tick);
    }

    _stopLoop() {
        if (this._raf !== null) cancelAnimationFrame(this._raf);
        this._raf = null;
        this._pause();
    }

    _scheduleRender() {
        if (this._raf === null) this._startLoop();
    }

    /* ---- rendering (port of helpers.ts::renderMap) ---- */

    _themeIsDark() {
        const bg = getComputedStyle(this._card || this).backgroundColor || "";
        const m = bg.match(/\d+/g);
        if (!m || m.length < 3) return true;
        // Rec. 601 luma — good enough to pick the light/dark colour set.
        const luma = (0.299 * +m[0] + 0.587 * +m[1] + 0.114 * +m[2]) / 255;
        return luma < 0.5;
    }

    // World extent of the floorplan's *drawn* content.
    //
    // A plan is usually a large canvas with the house somewhere inside it —
    // this one is 16.5 m square for a 5x7 m flat — so framing on the whole
    // image would shrink the map to a stamp. Instead, find the bounding box
    // of the pixels that differ from the background (taken from the corners,
    // since these exports have no alpha channel) and use that. It is a
    // property of the plan alone, so it does not move between sessions.
    // Returns null if the scan finds nothing, and the caller falls back to
    // session framing.
    _planContentBounds(img, fp) {
        if (!(fp.scale > 0) || !img.width || !img.height) return null;
        let data;
        try {
            const c = document.createElement("canvas");
            c.width = img.width;
            c.height = img.height;
            const cx = c.getContext("2d", { willReadFrequently: true });
            cx.drawImage(img, 0, 0);
            data = cx.getImageData(0, 0, c.width, c.height).data;
        } catch (err) {
            console.warn("openneato-replay-card: cannot inspect floorplan", err);
            return null;
        }

        const W = img.width;
        const H = img.height;
        const at = (x, y) => (y * W + x) * 4;
        // Background = median-ish of the four corners, which are margin on
        // every plan export I've seen.
        const corners = [at(0, 0), at(W - 1, 0), at(0, H - 1), at(W - 1, H - 1)];
        const bg = [0, 1, 2].map((ch) => {
            const v = corners.map((o) => data[o + ch]).sort((a, b) => a - b);
            return (v[1] + v[2]) / 2;
        });
        const TOL = 24;

        let x0 = W;
        let y0 = H;
        let x1 = -1;
        let y1 = -1;
        // Every second pixel is plenty for a bounding box and quarters the work.
        for (let y = 0; y < H; y += 2) {
            for (let x = 0; x < W; x += 2) {
                const o = at(x, y);
                if (data[o + 3] < 8) continue; // transparent margin, if any
                if (
                    Math.abs(data[o] - bg[0]) <= TOL &&
                    Math.abs(data[o + 1] - bg[1]) <= TOL &&
                    Math.abs(data[o + 2] - bg[2]) <= TOL
                ) {
                    continue;
                }
                if (x < x0) x0 = x;
                if (x > x1) x1 = x;
                if (y < y0) y0 = y;
                if (y > y1) y1 = y;
            }
        }
        if (x1 < x0 || y1 < y0) return null;

        // Pixels -> world metres. The image's bottom-left corner is the world
        // origin, and image rows run downward while world Y runs up.
        const s = fp.scale;
        return {
            minX: fp.originX + x0 / s,
            maxX: fp.originX + (x1 + 1) / s,
            minY: fp.originY + (H - 1 - y1) / s,
            maxY: fp.originY + (H - y0) / s,
        };
    }

    // Bounds the view is fitted to.
    //
    // The background is drawn in world coordinates, so it only appears to
    // move because the *frame* moves: fitting to the cleaned area means every
    // cleaning gets its own viewport. Snapping the frame outward to a fixed
    // world grid absorbs the half-metre of session-to-session variation, and
    // the plan then lands on the same pixels every time.
    _viewBounds(sessionBounds) {
        const mode = this._config.fit;
        if (mode === "session") return sessionBounds;

        let b = sessionBounds;
        let anchored = false;
        const plan = this._floorplanBounds;
        if (mode === "plan" && plan && this._floorplanImg) {
            // Keep the whole plan in view. Only worth it when the plan is
            // calibrated onto the cleaned area — otherwise it zooms out to
            // cover blank margin.
            b = {
                minX: Math.min(b.minX, plan.minX),
                maxX: Math.max(b.maxX, plan.maxX),
                minY: Math.min(b.minY, plan.minY),
                maxY: Math.max(b.maxY, plan.maxY),
            };
            anchored = true;
        }
        // The metre grid exists to absorb the session-to-session wobble in
        // the cleaned-area bounds. Once the plan is in the union it is the
        // plan — which does not move — that sets the frame, so a whole metre
        // of outward snapping buys no steadiness and costs real scale: on
        // this map it inflated a 5.35 x 7.10 m view to 6 x 8 and threw away
        // 12% of the zoom. Snap finely there, and keep the metre for the
        // modes where the session bounds really do drive the frame.
        const q = anchored ? FIT_QUANTUM_ANCHORED_M : FIT_QUANTUM_M;
        return {
            minX: Math.floor(b.minX / q) * q,
            maxX: Math.ceil(b.maxX / q) * q,
            minY: Math.floor(b.minY / q) * q,
            maxY: Math.ceil(b.maxY / q) * q,
        };
    }

    _projection(displayW, displayH, bounds) {
        // The map is rotated about the canvas centre, so what has to fit is
        // the *rotated* bounding box, not the upright one. Solving for the
        // scale directly covers every angle: a quarter turn transposes the
        // box, and the few degrees that straighten a skewed map barely
        // shrink it.
        const rot = (this._rotationDeg() * Math.PI) / 180;
        const ac = Math.abs(Math.cos(rot));
        const as = Math.abs(Math.sin(rot));
        const worldW = bounds.maxX - bounds.minX;
        const worldH = bounds.maxY - bounds.minY;
        const availW = displayW - MAP_PAD * 2;
        const availH = displayH - MAP_PAD * 2;
        const scale = Math.min(
            availW / (worldW * ac + worldH * as),
            availH / (worldW * as + worldH * ac),
        );
        // Centre the upright content on the canvas centre, which is the pivot.
        const offX = displayW / 2 - (worldW * scale) / 2;
        const offY = displayH / 2 - (worldH * scale) / 2;
        return {
            scale,
            toX: (wx) => offX + (wx - bounds.minX) * scale,
            toY: (wy) => offY + (bounds.maxY - wy) * scale,
        };
    }

    // Map orientation in degrees.
    //
    // A LIDAR-built plan is drawn in the robot's frame, whose heading comes
    // from the dock rather than the walls — and that frame can be re-zeroed,
    // so pinning a fixed number here would let the map tip over one day. With
    // "auto" the server derives the upright angle from the walls themselves
    // and the map always comes out the same way up.
    // The upright angle alone, before the quarter turn below is folded in.
    _baseRotationDeg() {
        const configured = this._config.rotation;
        if (configured === "auto" || configured === undefined) {
            const fp = this._floorplan;
            if (fp && Number.isFinite(fp.viewRotation)) return fp.viewRotation;
            return configured === "auto" ? 0 : Number(DEFAULTS.rotation) || 0;
        }
        return Number(configured) || 0;
    }

    _rotationDeg() {
        return this._baseRotationDeg() + (this._autoQuarter || 0);
    }

    // Quarter turn that makes the map fill the card.
    //
    // The viewport is fitted to the *rotated* bounding box, so a run that is
    // taller than the canvas is wide gets scaled down until it fits the short
    // axis, leaving the long axis half empty. Turning it a quarter lines the
    // long side of the run up with the long side of the card.
    //
    // Which way round that is depends on the card's shape, and this one is
    // laid out in a fluid grid column: measured on a 1920px window the turn
    // buys ~30% more scale, but at 1280px it *costs* 14%. So rather than
    // guess from the run's aspect alone, solve both orientations and keep the
    // one that actually zooms in more. The margin keeps a near-square run
    // from flipping back and forth as the window is resized.
    //
    // While a session is live its bounds grow, and a growing box can flip
    // the aspect ratio mid-clean — the map would turn a quarter under the
    // user. So the decision is frozen for the life of a live session (per
    // canvas size, so a real resize still re-fits) and taken again, once,
    // when the recording completes.
    _updateAutoQuarter(displayW, displayH, bounds) {
        const configured = this._config.rotation;
        if (configured !== "auto" && configured !== undefined) {
            this._autoQuarter = 0;
            return;
        }
        const lockKey = `${displayW}x${displayH}`;
        if (this._live && this._quarterLock === lockKey) return;
        this._quarterLock = this._live ? lockKey : null;
        const worldW = bounds.maxX - bounds.minX;
        const worldH = bounds.maxY - bounds.minY;
        if (!(worldW > 0) || !(worldH > 0)) {
            this._autoQuarter = 0;
            return;
        }
        const availW = displayW - MAP_PAD * 2;
        const availH = displayH - MAP_PAD * 2;
        const fitAt = (deg) => {
            const r = (deg * Math.PI) / 180;
            const ac = Math.abs(Math.cos(r));
            const as = Math.abs(Math.sin(r));
            return Math.min(
                availW / (worldW * ac + worldH * as),
                availH / (worldW * as + worldH * ac),
            );
        };
        const base = this._baseRotationDeg();
        this._autoQuarter = fitAt(base + 90) > fitAt(base) * 1.02 ? 90 : 0;
    }

    // Apply dpr, rotation and pan/zoom to a context, in that order, so the
    // coverage layer and the main canvas share one coordinate space.
    // A screen-space vector carried back through the view rotation, into the
    // frame the pan is expressed in.
    _unrotate(x, y) {
        const r = (-this._rotationDeg() * Math.PI) / 180;
        const c = Math.cos(r);
        const s = Math.sin(r);
        return { x: x * c - y * s, y: x * s + y * c };
    }

    // One square of the lattice, in device pixels.
    //
    // The period tracks the robot's own 5 cm occupancy cell rather than being
    // a fixed number of screen pixels, so one square means one cell and the
    // grid grows when you zoom in. A fixed period looked wrong the moment the
    // view was magnified — the walls got bigger and bigger while the texture
    // stayed the same fineness, which is the "ugly on zoom" complaint — and
    // it also made the coverage overstate itself, because a 5 cm cell spilled
    // across two or three squares and lit them all.
    _lattice(dpr, proj) {
        const cellM = (this._session && this._session.cellSize) || DEFAULT_CELL_M;
        const period = Math.max(2, Math.round(cellM * proj.scale * this._tf.zoom * dpr));
        const gutter = Math.max(1, Math.round(period * GRID_GUTTER_RATIO));
        return { period, size: Math.max(1, period - gutter) };
    }

    _applyTransform(ctx, dpr, displayW, displayH) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        const rotation = this._rotationDeg();
        if (rotation) {
            ctx.translate(displayW / 2, displayH / 2);
            ctx.rotate((rotation * Math.PI) / 180);
            ctx.translate(-displayW / 2, -displayH / 2);
        }
        ctx.translate(this._tf.panX, this._tf.panY);
        ctx.scale(this._tf.zoom, this._tf.zoom);
    }

    _render() {
        const canvas = this._canvas;
        if (!canvas) return;
        const displayW = canvas.clientWidth;
        const displayH = canvas.clientHeight;
        if (displayW === 0 || displayH === 0) return;

        const dpr = window.devicePixelRatio || 1;
        const wantW = Math.round(displayW * dpr);
        const wantH = Math.round(displayH * dpr);
        // Assigning width/height clears the canvas, so only do it on a real
        // size change — otherwise every frame would pay a full reallocation.
        if (canvas.width !== wantW || canvas.height !== wantH) {
            canvas.width = wantW;
            canvas.height = wantH;
        }

        const ctx = canvas.getContext("2d");
        const isDark = this._themeIsDark();

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        // White, not a tinted grey. The map reads as three states and this is
        // one of them: black is wall, blue is cleaned, and whatever shows
        // through the lattice gaps and around the plan is empty space.
        ctx.fillStyle = isDark ? "#1a1a1c" : "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        const session = this._session;
        if (!session || !session.bounds) return;

        // The quarter turn is derived from the fitted bounds, and every
        // transform below reads it back through _rotationDeg(), so settle it
        // before the first one is applied.
        const bounds = this._viewBounds(session.bounds);
        this._updateAutoQuarter(displayW, displayH, bounds);

        this._applyTransform(ctx, dpr, displayW, displayH);
        const proj = this._projection(displayW, displayH, bounds);
        const tNow = this._time;

        // Grid first, and square to the screen rather than to the world: once
        // the plan is straightened against the walls, a grid still aligned to
        // the robot's frame is the only thing left leaning over.
        if (this._look.lines) this._drawGrid(ctx, proj, dpr, displayW, displayH, isDark);

        // Coverage first, walls second.
        //
        // Cleaning is not an overlay on the map any more, it *is* the map: a
        // square starts empty and turns blue when the robot has been over it,
        // on the same lattice the walls are drawn on. So it goes down before
        // the plan, and the walls land on top and stay readable where the
        // robot brushed against them.
        this._paintCoverage(ctx, proj, dpr, displayW, displayH, tNow, isDark, bounds);

        // The track sits on the cleaned floor it just laid down, and under
        // the walls, so brushing along a wall never rubs the wall out.
        if (this._config.trail ?? DEFAULTS.trail) {
            this._paintHistory(ctx, proj, dpr, displayW, displayH, tNow, bounds);
            this._drawTrail(ctx, proj, dpr, displayW, displayH, tNow);
        }

        this._applyTransform(ctx, dpr, displayW, displayH);
        this._drawFloorplan(ctx, proj, dpr, displayW, displayH);

        this._applyTransform(ctx, dpr, displayW, displayH);
        const head = session.interpolate(tNow);
        this._drawMarkers(ctx, proj, head, isDark);
        this._drawRecharges(ctx, proj, tNow, isDark);

        this._syncControls();
    }

    // Everywhere the robot has been, at a constant faint tone.
    //
    // This is the half of the track that never fades: by the end of a run you
    // can read the whole route it took. It is built incrementally like the
    // coverage — appended as the playhead advances rather than redrawn — and
    // each square is painted once and remembered, because the robot crosses
    // its own route constantly and a translucent fill laid down twice comes
    // out twice as dark.
    _paintHistory(ctx, proj, dpr, displayW, displayH, tNow, bounds) {
        if (!this._look.tiles) {
            this._strokeTrack(ctx, proj, dpr, displayW, displayH, tNow);
            return;
        }
        const session = this._session;
        const sig = [
            dpr, displayW, displayH,
            this._rotationDeg(), this._tf.panX, this._tf.panY, this._tf.zoom,
            session.name,
            bounds.minX, bounds.maxX, bounds.minY, bounds.maxY,
        ].join("|");

        let layer = this._trk.canvas;
        if (this._trk.sig !== sig || !layer) {
            layer = document.createElement("canvas");
            layer.width = Math.round(displayW * dpr);
            layer.height = Math.round(displayH * dpr);
            this._trk.canvas = layer;
            this._trk.ctx = layer.getContext("2d");
            this._trk.sig = sig;
            this._trk.cursor = 0;
            this._trk.time = -1;
            this._trk.painted = new Set();
            this._applyTransform(this._trk.ctx, dpr, displayW, displayH);
            this._trk.matrix = this._trk.ctx.getTransform();
            this._trk.ctx.setTransform(1, 0, 0, 1, 0, 0);
            this._trk.ctx.fillStyle = TRAIL_COLOR;
            this._trk.ctx.globalAlpha = TRAIL_PERSIST_ALPHA;
        }

        const lctx = this._trk.ctx;
        if (tNow < this._trk.time) {
            lctx.clearRect(0, 0, layer.width, layer.height);
            this._trk.painted.clear();
            this._trk.cursor = 0;
        }

        const { period, size } = this._lattice(dpr, proj);
        const matrix = this._trk.matrix;
        const painted = this._trk.painted;
        const at = (i) =>
            matrix.transformPoint(
                new DOMPoint(proj.toX(session.path[i * 4]), proj.toY(session.path[i * 4 + 1])),
            );
        const lay = (px, py) => {
            const gx = Math.floor(px / period) * period;
            const gy = Math.floor(py / period) * period;
            const k = gy * 100000 + gx;
            if (painted.has(k)) return;
            painted.add(k);
            lctx.fillRect(gx, gy, size, size);
        };

        const n = session.poseCountUpTo(tNow);
        let i = Math.max(1, this._trk.cursor);
        if (this._trk.cursor === 0 && n > 0) lay(at(0).x, at(0).y);
        for (; i < n; i++) {
            const a = at(i - 1);
            const b = at(i);
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / (period / 2)));
            for (let s = 1; s <= steps; s++) {
                lay(a.x + (dx * s) / steps, a.y + (dy * s) / steps);
            }
        }
        this._trk.cursor = n;
        this._trk.time = tNow;

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(layer, 0, 0);
    }

    // Width of the smooth track in the transformed frame: one cell, but never
    // thinner than 1.5 CSS px on screen.
    _trackWidth(proj) {
        const cellM = (this._session && this._session.cellSize) || DEFAULT_CELL_M;
        return Math.max(1.5 / this._tf.zoom, cellM * proj.scale);
    }

    // Flat look: the whole route so far as one faint rounded polyline. A
    // single stroke composites once even where the path crosses itself, so
    // nothing has to be deduplicated and no offscreen layer is needed; a
    // one-hour run is under two thousand points, cheap to re-stroke.
    _strokeTrack(ctx, proj, dpr, displayW, displayH, tNow) {
        const session = this._session;
        const n = session.poseCountUpTo(tNow);
        if (n === 0) return;
        ctx.save();
        this._applyTransform(ctx, dpr, displayW, displayH);
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.strokeStyle = TRAIL_COLOR;
        ctx.globalAlpha = TRAIL_PERSIST_ALPHA;
        ctx.lineWidth = this._trackWidth(proj);
        ctx.beginPath();
        const x0 = proj.toX(session.path[0]);
        const y0 = proj.toY(session.path[1]);
        ctx.moveTo(x0, y0);
        // A lone pose still gets a dot from the round cap.
        if (n === 1) ctx.lineTo(x0, y0);
        for (let i = 1; i < n; i++) {
            ctx.lineTo(proj.toX(session.path[i * 4]), proj.toY(session.path[i * 4 + 1]));
        }
        ctx.stroke();
        ctx.restore();
    }

    // Flat look: the last TRAIL_SECONDS of the path, one short stroke per
    // segment fading from the head, drawn oldest first so the head lands on
    // top. Wider than the track so it sits visibly over the faint trace.
    _strokeComet(ctx, proj, dpr, displayW, displayH, tNow) {
        const session = this._session;
        const n = session.poseCountUpTo(tNow);
        if (n < 2) return;
        const oldest = tNow - TRAIL_SECONDS;
        let start = n - 1;
        while (start > 0 && session.poseTs(start - 1) >= oldest) start--;
        ctx.save();
        this._applyTransform(ctx, dpr, displayW, displayH);
        ctx.lineCap = "round";
        ctx.strokeStyle = TRAIL_COLOR;
        ctx.lineWidth = this._trackWidth(proj) * 1.6;
        for (let i = start; i < n - 1; i++) {
            const weight = 1 - (tNow - session.poseTs(i)) / TRAIL_SECONDS;
            if (weight <= 0) continue;
            ctx.globalAlpha = weight;
            ctx.beginPath();
            ctx.moveTo(proj.toX(session.path[i * 4]), proj.toY(session.path[i * 4 + 1]));
            ctx.lineTo(proj.toX(session.path[(i + 1) * 4]), proj.toY(session.path[(i + 1) * 4 + 1]));
            ctx.stroke();
        }
        ctx.restore();
    }

    // The robot's recent track, as a comet fading into the permanent trace.
    //
    // A polyline would have been the easy answer and the wrong one: the whole
    // map is a lattice of squares, and a smooth stroke laid over it reads as
    // a different drawing pasted on top — which is exactly what we just took
    // out. So the track is drawn in the same squares, on the same grid, and
    // carries its meaning in depth of colour instead: the square the robot is
    // on is darkest, and the further back you look the closer it settles onto
    // the faint permanent trace underneath.
    //
    // Ages are measured in *session* seconds, not wall-clock, so the comet is
    // the same length whether the replay is running at 1x or 64x — it always
    // shows the last TRAIL_SECONDS of the robot's afternoon.
    //
    // Squares are deduplicated on their strongest value before anything is
    // drawn. The robot logs a couple of poses a second and barely moves while
    // it turns, so without this a pause would stack dozens of translucent
    // fills on one square and burn it darker than the head of the comet.
    _drawTrail(ctx, proj, dpr, displayW, displayH, tNow) {
        if (!this._look.tiles) {
            this._strokeComet(ctx, proj, dpr, displayW, displayH, tNow);
            return;
        }
        const session = this._session;
        const n = session.poseCountUpTo(tNow);
        if (n === 0) return;

        const { period, size } = this._lattice(dpr, proj);

        ctx.save();
        this._applyTransform(ctx, dpr, displayW, displayH);
        const matrix = ctx.getTransform();
        ctx.setTransform(1, 0, 0, 1, 0, 0);

        const oldest = tNow - TRAIL_SECONDS;
        const strongest = new Map();
        const mark = (px, py, weight) => {
            const gx = Math.floor(px / period) * period;
            const gy = Math.floor(py / period) * period;
            const k = gy * 100000 + gx;
            const prev = strongest.get(k);
            if (prev === undefined || weight > prev.w) {
                strongest.set(k, { x: gx, y: gy, w: weight });
            }
        };
        const at = (i) =>
            matrix.transformPoint(
                new DOMPoint(proj.toX(session.path[i * 4]), proj.toY(session.path[i * 4 + 1])),
            );

        // Walk the segments, not the poses. The robot logs a pose about every
        // two seconds and covers some 9 cm between them — wider than a square
        // — so marking poses alone draws a dotted line. Stepping along each
        // segment at half a square lays it down as one ribbon.
        let head = at(n - 1);
        for (let i = n - 1; i > 0; i--) {
            const ts = session.poseTs(i - 1);
            if (ts < oldest) break;
            const weight = 1 - (tNow - ts) / TRAIL_SECONDS;
            if (weight <= 0) continue;
            const tail = at(i - 1);
            const dx = tail.x - head.x;
            const dy = tail.y - head.y;
            const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / (period / 2)));
            for (let s = 0; s <= steps; s++) {
                mark(head.x + (dx * s) / steps, head.y + (dy * s) / steps, weight);
            }
            head = tail;
        }

        ctx.fillStyle = TRAIL_COLOR;
        for (const sq of strongest.values()) {
            // Fades to nothing on purpose: the permanent trace is already
            // painted underneath, so the comet dissolving into it is what
            // makes the two read as one track rather than as two.
            ctx.globalAlpha = sq.w * sq.w;
            ctx.fillRect(sq.x, sq.y, size, size);
        }
        ctx.restore();
    }

    // Snap a drawn layer onto the lattice: one flat square per cell, one
    // colour each, with a transparent gutter between them.
    _quantiseToLattice(cx, width, height, lattice) {
        const { period, size } = lattice;
        const src = cx.getImageData(0, 0, width, height);
        const dst = cx.createImageData(width, height);
        const s = src.data;
        const d = dst.data;

        for (let y0 = 0; y0 < height; y0 += period) {
            for (let x0 = 0; x0 < width; x0 += period) {
                // Sample the square's centre — the same rule the coverage
                // layer uses in _resolveToLattice().
                //
                // This used to light the square if *any* pixel in it was
                // wall. That made sense while the plan carried a floor tint
                // the walls had to win against, but the plan is walls-only
                // now, so all it did was dilate every wall by up to a square
                // while the coverage, sampled at its centre, eroded by half
                // of one. Along every wall face that read as five to eight
                // centimetres of the cleaned area disappearing under the
                // wall — the two layers drawn to different rules, not a
                // misalignment in the data underneath.
                const mid = size >> 1;
                const sy = Math.min(y0 + mid, height - 1);
                const sx = Math.min(x0 + mid, width - 1);
                const o = (sy * width + sx) * 4;
                if (s[o + 3] < 128) continue;
                const r = s[o];
                const g = s[o + 1];
                const b = s[o + 2];
                for (let y = y0; y < y0 + size && y < height; y++) {
                    const row = y * width;
                    for (let x = x0; x < x0 + size && x < width; x++) {
                        const o = (row + x) * 4;
                        d[o] = r;
                        d[o + 1] = g;
                        d[o + 2] = b;
                        d[o + 3] = 255;
                    }
                }
            }
        }
        cx.putImageData(dst, 0, 0);
    }

    // The plan on its own layer, quantised onto a screen-aligned lattice.
    //
    // Two things have to be true for this to read as a grid of squares, and
    // getting either wrong is what produced the earlier messes:
    //
    // 1. The lattice must be applied *after* every transform. The card
    //    rescales the plan by a fractional factor and turns it a couple of
    //    degrees to straighten it against the walls, and either operation
    //    resamples a lattice baked into the PNG into ragged clumps — squares
    //    come out as L-shapes and pairs weld together.
    // 2. Each square must take a single colour. Masking the drawn plan
    //    through a lattice is not enough: the wall cells underneath do not
    //    line up with it, so one square can straddle a wall edge and come out
    //    part black, part blue. So every square is resolved to one of the
    //    three states and filled flat.
    //
    // Walls win a square they only partly cover. They are the thin feature
    // here, and letting the floor take those squares eats holes in them.
    //
    // Cached against everything that moves the plan on screen, so this costs
    // one pass per zoom or pan rather than one per frame.
    _planLayer(img, fp, proj, dpr, displayW, displayH) {
        const ax = proj.toX(fp.originX);
        const ay = proj.toY(fp.originY);
        // Every term of _applyTransform has to appear here. Leaving the pan
        // out meant dragging the map moved the coverage — which is redrawn
        // each frame — while the cached walls stayed where they were.
        const key = [
            this._floorplanKey,
            Math.round(displayW * dpr),
            Math.round(displayH * dpr),
            proj.scale.toFixed(3),
            this._rotationDeg().toFixed(3),
            this._tf.panX.toFixed(1),
            this._tf.panY.toFixed(1),
            this._tf.zoom.toFixed(3),
            ax.toFixed(1),
            ay.toFixed(1),
        ].join("|");
        if (this._planKey === key && this._planCanvas) return this._planCanvas;

        const c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(displayW * dpr));
        c.height = Math.max(1, Math.round(displayH * dpr));
        const cx = c.getContext("2d", { willReadFrequently: true });
        this._applyTransform(cx, dpr, displayW, displayH);

        const factor = proj.scale / fp.scale;
        const w = img.width * factor;
        const h = img.height * factor;
        // No smoothing: interpolation invents colours between the three the
        // plan actually uses, and those blends survive quantising.
        cx.imageSmoothingEnabled = false;
        cx.translate(ax, ay);
        if (fp.rotation) cx.rotate((fp.rotation * Math.PI) / 180);
        cx.drawImage(img, 0, -h, w, h);

        cx.setTransform(1, 0, 0, 1, 0, 0);
        this._quantiseToLattice(cx, c.width, c.height, this._lattice(dpr, proj));

        this._planKey = key;
        this._planCanvas = c;
        return c;
    }

    _drawFloorplan(ctx, proj, dpr, displayW, displayH) {
        const img = this._floorplanImg;
        const fp = this._floorplan;
        if (!img || !fp || !(fp.scale > 0)) return;
        const alpha = Number(this._config.floorplan_opacity ?? 1);

        // Screened: the layer is already in screen pixels, so blit it flat.
        if (this._look.plan && dpr) {
            const layer = this._planLayer(img, fp, proj, dpr, displayW, displayH);
            ctx.save();
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.globalAlpha = alpha;
            ctx.drawImage(layer, 0, 0);
            ctx.restore();
            return;
        }

        // fp.scale is the plan's own pixels per metre; the session projection
        // is px per metre on screen. Resizing by their ratio makes the plan's
        // geometry line up with the robot's world coordinates.
        const factor = proj.scale / fp.scale;
        const w = img.width * factor;
        const h = img.height * factor;
        // `origin` is the world point at the image's BOTTOM-left corner, and
        // world Y grows upward while canvas Y grows down.
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.translate(proj.toX(fp.originX), proj.toY(fp.originY));
        if (fp.rotation) ctx.rotate((fp.rotation * Math.PI) / 180);
        ctx.drawImage(img, 0, -h, w, h);
        ctx.restore();
    }

    // Half-metre grid, drawn square to the screen and covering the whole
    // canvas.
    //
    // It used to be laid out along the world axes, which was right while the
    // map itself sat at whatever angle the dock imposed. Now that the view is
    // straightened against the walls, a world-aligned grid is the one thing
    // still leaning. So the lines are drawn in screen space, anchored on
    // wherever the world origin lands once pan, zoom and rotation are
    // applied — that keeps the grid locked to the map when panning instead of
    // swimming across it.
    _drawGrid(ctx, proj, dpr, displayW, displayH, isDark) {
        const zoom = this._tf.zoom;
        const step = GRID_STEP * proj.scale * zoom;
        if (step < 6) return; // denser than this is just noise

        const rot = (this._rotationDeg() * Math.PI) / 180;
        const c = Math.cos(rot);
        const s = Math.sin(rot);
        // Same order the canvas applies: zoom, then pan, then rotate about
        // the canvas centre.
        const px = proj.toX(0) * zoom + this._tf.panX;
        const py = proj.toY(0) * zoom + this._tf.panY;
        const ax = displayW / 2 + (px - displayW / 2) * c - (py - displayH / 2) * s;
        const ay = displayH / 2 + (px - displayW / 2) * s + (py - displayH / 2) * c;

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);
        ctx.strokeStyle = isDark ? "rgba(255, 255, 255, 0.04)" : "rgba(0, 0, 0, 0.06)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = ax - Math.ceil(ax / step) * step; x <= displayW; x += step) {
            ctx.moveTo(x, 0);
            ctx.lineTo(x, displayH);
        }
        for (let y = ay - Math.ceil(ay / step) * step; y <= displayH; y += step) {
            ctx.moveTo(0, y);
            ctx.lineTo(displayW, y);
        }
        ctx.stroke();
    }

    // Resolve one patch of a continuous shape onto the lattice: a square is
    // lit when the shape covers its centre. Only the given box is looked at,
    // so this is a small read even though the layers are canvas-sized.
    _resolveToLattice(rctx, lctx, layer, period, size, x0, y0, x1, y1) {
        const bx = Math.max(0, Math.floor(x0 / period) * period);
        const by = Math.max(0, Math.floor(y0 / period) * period);
        const bw = Math.min(layer.width, Math.ceil(x1 / period) * period + period) - bx;
        const bh = Math.min(layer.height, Math.ceil(y1 / period) * period + period) - by;
        if (bw <= 0 || bh <= 0) return;

        const src = rctx.getImageData(bx, by, bw, bh).data;
        const mid = size >> 1;
        for (let gy = 0; gy + size <= bh; gy += period) {
            for (let gx = 0; gx + size <= bw; gx += period) {
                if (src[((gy + mid) * bw + gx + mid) * 4 + 3] < 128) continue;
                lctx.fillRect(bx + gx, by + gy, size, size);
            }
        }
    }

    // Coverage is the expensive part — tens of thousands of cells. Because
    // it only ever grows as the playhead advances, it is drawn once into an
    // offscreen layer and appended to frame by frame; a seek backwards or a
    // change of view transform rebuilds it from scratch.
    //
    // Flat look (`tiled_coverage: false`): the continuous shape is the layer,
    // in the coverage colour, and the lattice pass is skipped. Everything
    // else — cursor, time, the seek-back rebuild — is shared.
    _paintCoverage(ctx, proj, dpr, displayW, displayH, tNow, isDark, bounds) {
        const session = this._session;
        const tiled = this._look.tiles;
        // The framing is part of the signature: the floorplan loads
        // asynchronously, so the first frame may be drawn on session bounds
        // and the next on plan bounds. Without this the layer would keep the
        // stale projection and the coverage would sit off the path.
        const sig = [
            dpr, displayW, displayH, isDark, tiled,
            this._rotationDeg(), this._tf.panX, this._tf.panY, this._tf.zoom,
            session.name,
            bounds.minX, bounds.maxX, bounds.minY, bounds.maxY,
        ].join("|");

        let layer = this._cov.canvas;
        if (this._cov.sig !== sig || !layer) {
            const w = Math.round(displayW * dpr);
            const h = Math.round(displayH * dpr);
            // Two layers. `raw` holds the cleaned area as a continuous shape,
            // drawn through the ordinary transform; `canvas` is that shape
            // resolved onto the lattice.
            //
            // Snapping each cell's centre to the nearest square instead —
            // which is what this did — beats itself against the lattice: a
            // cell is 3.77 device pixels and a square 4, so roughly one cell
            // in seventeen finds no square of its own, and those misses line
            // up into the pale criss-cross that appeared over the floor. No
            // integer period can fix it either, because the map is turned a
            // couple of degrees and the cell grid does not run square to the
            // screen. Sampling a continuous shape has no such beat: the
            // squares are decided by area, not by hitting a point.
            const raw = document.createElement("canvas");
            raw.width = w;
            raw.height = h;
            this._cov.raw = raw;
            this._cov.rawCtx = raw.getContext("2d", { willReadFrequently: tiled });
            if (tiled) {
                layer = document.createElement("canvas");
                layer.width = w;
                layer.height = h;
                this._cov.ctx = layer.getContext("2d");
                this._cov.ctx.fillStyle = COVERAGE_COLOR;
            } else {
                layer = raw;
                this._cov.ctx = null;
            }
            this._cov.canvas = layer;
            this._cov.sig = sig;
            this._cov.cursor = 0;
            this._cov.time = -1;
            this._applyTransform(this._cov.rawCtx, dpr, displayW, displayH);
            // Recapture it here, or the next frame reuses the matrix of the
            // view we just replaced.
            this._cov.matrix = this._cov.rawCtx.getTransform();
            this._cov.rawCtx.fillStyle = tiled ? "#000" : COVERAGE_COLOR;
        }

        const lctx = this._cov.ctx;
        const rctx = this._cov.rawCtx;
        if (tNow < this._cov.time) {
            // Seeking backwards. The raw layer still carries the world
            // transform, so clear it in device space and put it back.
            rctx.save();
            rctx.setTransform(1, 0, 0, 1, 0, 0);
            rctx.clearRect(0, 0, layer.width, layer.height);
            rctx.restore();
            if (lctx) lctx.clearRect(0, 0, layer.width, layer.height);
            this._cov.cursor = 0;
        }

        // Append the newly cleaned cells to the continuous shape, keeping the
        // device-space box they touched so only that part has to be resolved
        // onto the lattice afterwards. The robot covers very little ground
        // between two frames, so this stays a small patch.
        const { period, size } = this._lattice(dpr, proj);
        const matrix = this._cov.matrix || rctx.getTransform();
        this._cov.matrix = matrix;
        const cells = session.coverage;
        const cellPx = session.cellSize * proj.scale;
        // Flat cells get half a device pixel on each side, so neighbours
        // overlap instead of leaving anti-aliased hairline seams between
        // them; the result is one solid swept area with soft outer edges.
        const grow = tiled ? 0 : 1 / (dpr * this._tf.zoom);
        const half = cellPx / 2 + grow / 2;
        const side = cellPx + grow;
        const reach = (cellPx * this._tf.zoom * dpr) / 2 + period;
        let i = this._cov.cursor;
        const n = session.cellCount;
        let x0 = Infinity;
        let y0 = Infinity;
        let x1 = -Infinity;
        let y1 = -Infinity;
        while (i < n && cells[i * 3 + 2] <= tNow) {
            const wx = cells[i * 3] * session.cellSize;
            const wy = cells[i * 3 + 1] * session.cellSize;
            const cxp = proj.toX(wx);
            const cyp = proj.toY(wy);
            rctx.fillRect(cxp - half, cyp - half, side, side);
            const p = matrix.transformPoint(new DOMPoint(cxp, cyp));
            if (p.x - reach < x0) x0 = p.x - reach;
            if (p.y - reach < y0) y0 = p.y - reach;
            if (p.x + reach > x1) x1 = p.x + reach;
            if (p.y + reach > y1) y1 = p.y + reach;
            i++;
        }
        if (tiled && i > this._cov.cursor) {
            this._resolveToLattice(rctx, lctx, layer, period, size, x0, y0, x1, y1);
        }
        this._cov.cursor = i;
        this._cov.time = tNow;

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(layer, 0, 0);
    }

    _drawMarkers(ctx, proj, head) {
        const session = this._session;
        if (session.poseCount === 0) return;

        ctx.beginPath();
        ctx.arc(proj.toX(session.path[0]), proj.toY(session.path[1]), 5, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(52, 199, 89, 0.9)";
        ctx.fill();

        // A live session's newest frame is the robot, not a finish line.
        const atEnd = !this._live && this._time >= session.duration;
        if (!atEnd && head) {
            this._drawRobot(ctx, proj.toX(head.x), proj.toY(head.y), head.t);
            return;
        }
        const last = (session.poseCount - 1) * 4;
        ctx.beginPath();
        ctx.arc(proj.toX(session.path[last]), proj.toY(session.path[last + 1]), 5, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(255, 69, 58, 0.9)";
        ctx.fill();
    }

    // Filled circle with a heading wedge. Theta is degrees, counter-clockwise
    // in world coords, so the sign flips on screen (toY inverts Y).
    _drawRobot(ctx, x, y, thetaDeg) {
        const radius = 7;
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(-(thetaDeg * Math.PI) / 180);

        ctx.beginPath();
        ctx.moveTo(radius + 5, 0);
        ctx.lineTo(radius * 0.6, -radius * 0.7);
        ctx.lineTo(radius * 0.6, radius * 0.7);
        ctx.closePath();
        ctx.fillStyle = "rgba(52, 199, 89, 0.95)";
        ctx.fill();

        ctx.shadowColor = "rgba(52, 199, 89, 0.6)";
        ctx.shadowBlur = 10;
        ctx.beginPath();
        ctx.arc(0, 0, radius, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(52, 199, 89, 0.95)";
        ctx.fill();
        ctx.shadowBlur = 0;

        ctx.beginPath();
        ctx.arc(0, 0, radius * 0.35, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
        ctx.fill();
        ctx.restore();
    }

    _drawRecharges(ctx, proj, tNow, isDark) {
        for (const rp of this._session.recharges) {
            if (rp.ts > tNow) continue;
            const rx = proj.toX(rp.x);
            const ry = proj.toY(rp.y);
            const s = 10;
            const bolt = () => {
                ctx.beginPath();
                ctx.moveTo(rx + s * 0.15, ry - s);
                ctx.lineTo(rx - s * 0.55, ry + s * 0.05);
                ctx.lineTo(rx - s * 0.05, ry + s * 0.05);
                ctx.lineTo(rx - s * 0.15, ry + s);
                ctx.lineTo(rx + s * 0.55, ry - s * 0.05);
                ctx.lineTo(rx + s * 0.05, ry - s * 0.05);
                ctx.closePath();
            };
            ctx.save();
            ctx.shadowColor = "rgba(255, 204, 0, 0.7)";
            ctx.shadowBlur = 8;
            bolt();
            ctx.fillStyle = "rgba(255, 204, 0, 1)";
            ctx.fill();
            ctx.restore();
            bolt();
            ctx.strokeStyle = isDark ? "rgba(0, 0, 0, 0.5)" : "rgba(0, 0, 0, 0.3)";
            ctx.lineWidth = 1.5;
            ctx.stroke();
        }
    }

    /* ---- control sync ---- */

    _syncControls() {
        const duration = this._session.duration;
        this._elapsedEl.textContent = formatClock(this._time);
        // Don't fight the user while they drag the scrubber. Focus inside a
        // shadow root is only visible from that root — document.activeElement
        // reports the host.
        if (this.shadowRoot.activeElement !== this._scrub || this._playing) {
            this._scrub.value = String(this._time);
        }
        const played = duration > 0 ? (this._time / duration) * 100 : 0;
        this._scrub.style.setProperty("--played", `${played}%`);
        // Keyed on the duration as well: a live session grows, and the
        // recharge marks have to move with it.
        const trackKey = `${this._session.name}|${duration}`;
        if (this._trackFor !== trackKey) {
            this._trackFor = trackKey;
            this._scrub.style.setProperty("--track", this._buildTrack(duration));
        }
    }

    // Green for cleaning, yellow for charge windows — a glance at the bar
    // tells you where the robot went back to the dock.
    _buildTrack(duration) {
        if (duration <= 0) return "var(--divider-color)";
        const CLEAN = "rgba(52, 199, 89, 0.75)";
        const CHARGE = "rgba(255, 204, 0, 0.85)";
        const windows = this._session.recharges
            .map((r) => [
                Math.max(0, Math.min(duration, r.ts)),
                Math.max(0, Math.min(duration, r.endTs)),
            ])
            .filter(([a, b]) => b > a)
            .sort((a, b) => a[0] - b[0]);

        const stops = [];
        const pct = (t) => `${((t / duration) * 100).toFixed(3)}%`;
        const push = (color, from, to) => {
            stops.push(`${color} ${pct(from)}`, `${color} ${pct(to)}`);
        };
        let cursor = 0;
        for (const [from, to] of windows) {
            if (from > cursor) push(CLEAN, cursor, from);
            push(CHARGE, Math.max(cursor, from), to);
            cursor = to;
        }
        if (cursor < duration) push(CLEAN, cursor, duration);
        return `linear-gradient(to right, ${stops.join(", ")})`;
    }
}

customElements.define("openneato-replay-card", OpenNeatoReplayCard);

window.customCards = window.customCards || [];
window.customCards.push({
    type: "openneato-replay-card",
    name: "OpenNeato Replay",
    description: "Smooth canvas replay of a cleaning session, with scrubber and playback controls.",
    preview: false,
});

console.info(
    `%c OPENNEATO-REPLAY-CARD %c ${CARD_VERSION} `,
    "color: #1e1e22; background: #34c759; font-weight: 700;",
    "color: #34c759; background: #1e1e22;",
);
