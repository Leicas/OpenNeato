// Map data processing — parses raw JSONL from firmware and generates
// coverage maps, path arrays, and bounding boxes client-side.

import type { MapBounds, MapData, MapPathPoint, MapRechargePoint, MapSession, MapSummary } from "./types";

const ROBOT_DIAMETER_M = 0.33; // Neato Botvac diameter
const CELL_SIZE_M = 0.05; // 5cm grid cells for coverage map

interface RawPose {
    x: number;
    y: number;
    t: number;
    ts: number;
}

// Pose line structure: {"x":NUM,"y":NUM,"t":NUM,"ts":NUM}
// Heatshrink decompression can corrupt single bytes in numeric tokens
// (e.g. '.' -> ':' or '.' -> '"'). Try to recover by matching the structural
// skeleton permissively and replacing non-numeric garbage with '.'.
const POSE_RE =
    /^\{.x.:\s*(-?[\d.eE:"\w-]+)\s*,.y.:\s*(-?[\d.eE:"\w-]+)\s*,.t.:\s*([\d.eE:"\w-]+)\s*,.ts.:\s*([\d.eE:"\w-]+)\s*\}$/;

function repairNumber(raw: string): number {
    // Replace any character that isn't digit, dot, minus, or 'e'/'E' with '.'
    const cleaned = raw.replace(/[^0-9.eE-]/g, ".");
    // Collapse multiple dots — keep only the first as the decimal point
    const parts = cleaned.split(".");
    const fixed = parts.length > 2 ? `${parts[0]}.${parts.slice(1).join("")}` : cleaned;
    const n = Number(fixed);
    return Number.isFinite(n) ? n : Number.NaN;
}

function tryRepairPoseLine(line: string): RawPose | null {
    const m = line.match(POSE_RE);
    if (!m) return null;
    const x = repairNumber(m[1]);
    const y = repairNumber(m[2]);
    const t = repairNumber(m[3]);
    const ts = repairNumber(m[4]);
    if (Number.isNaN(x) || Number.isNaN(y) || Number.isNaN(t) || Number.isNaN(ts)) return null;
    return { x, y, t, ts };
}

/** Parse raw JSONL text (possibly multiple sessions) into MapData[] */
export function parseMapData(raw: string): MapData[] {
    const lines = raw
        .trim()
        .split("\n")
        .filter((l) => l.length > 0);

    // Split into per-session chunks: each starts with a {"type":"session",...} line
    const chunks: string[][] = [];
    let current: string[] = [];
    for (const line of lines) {
        // Peek at the line to detect session boundary
        if (line.includes('"type":"session"') && current.length > 0) {
            chunks.push(current);
            current = [];
        }
        current.push(line);
    }
    if (current.length > 0) chunks.push(current);

    return chunks.map(buildSession).filter((m) => m.path.length > 0);
}

// Union of all line types that can appear in a session JSONL
type SessionLine = MapSession | MapSummary | RawPose | (MapRechargePoint & { type: "recharge" });

function isSession(l: SessionLine): l is MapSession {
    return "type" in l && (l as MapSession).type === "session";
}
function isSummary(l: SessionLine): l is MapSummary {
    return "type" in l && (l as MapSummary).type === "summary";
}
function isRecharge(l: SessionLine): l is MapRechargePoint & { type: "recharge" } {
    return "type" in l && (l as { type: string }).type === "recharge";
}
function isPose(l: SessionLine): l is RawPose {
    return "x" in l && !("type" in l);
}

function buildSession(lines: string[]): MapData {
    const parsed: SessionLine[] = [];
    for (const l of lines) {
        try {
            parsed.push(JSON.parse(l));
        } catch {
            // Attempt to recover corrupted pose lines before dropping
            const repaired = tryRepairPoseLine(l);
            if (repaired) parsed.push(repaired);
        }
    }

    const session: MapSession | null = parsed.find(isSession) ?? null;
    const summary: MapSummary | null = parsed.find(isSummary) ?? null;
    const poses: RawPose[] = parsed.filter(isPose).filter((l) => l.x !== 0 || l.y !== 0 || l.t !== 0);
    const recharges: MapRechargePoint[] = parsed.filter(isRecharge).map((l) => ({ x: l.x, y: l.y }));

    if (poses.length === 0) {
        return { session, summary, path: [], coverage: [], recharges, bounds: null, cellSize: CELL_SIZE_M };
    }

    const path: MapPathPoint[] = poses.map((p) => ({ x: p.x, y: p.y, t: p.t, ts: p.ts }));

    // Coverage grid — stamp robot footprint circle at each pose
    const radiusCells = Math.ceil(ROBOT_DIAMETER_M / 2 / CELL_SIZE_M);
    const coveredCells = new Set<string>();

    for (const p of poses) {
        const cx = Math.round(p.x / CELL_SIZE_M);
        const cy = Math.round(p.y / CELL_SIZE_M);
        for (let dx = -radiusCells; dx <= radiusCells; dx++) {
            for (let dy = -radiusCells; dy <= radiusCells; dy++) {
                if (dx * dx + dy * dy <= radiusCells * radiusCells) {
                    coveredCells.add(`${cx + dx},${cy + dy}`);
                }
            }
        }
    }

    const coverage: [number, number][] = Array.from(coveredCells).map((k) => {
        const [c, r] = k.split(",").map(Number);
        return [c, r];
    });

    // Bounding box padded by robot radius
    const pad = ROBOT_DIAMETER_M / 2 + 0.1;
    const xs = poses.map((p) => p.x);
    const ys = poses.map((p) => p.y);
    const bounds: MapBounds = {
        minX: Math.min(...xs) - pad,
        maxX: Math.max(...xs) + pad,
        minY: Math.min(...ys) - pad,
        maxY: Math.max(...ys) + pad,
    };

    return { session, summary, path, coverage, recharges, bounds, cellSize: CELL_SIZE_M };
}
