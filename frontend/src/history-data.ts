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

function buildSession(lines: string[]): MapData {
    const parsed = lines.map((l) => JSON.parse(l));

    const session: MapSession | null = parsed.find((l) => l.type === "session") ?? null;
    const summary: MapSummary | null = parsed.find((l) => l.type === "summary") ?? null;
    const poses: RawPose[] = parsed.filter(
        (l: Record<string, unknown>) => l.x !== undefined && !l.type && (l.x !== 0 || l.y !== 0 || l.t !== 0),
    );
    const recharges: MapRechargePoint[] = parsed
        .filter((l: Record<string, unknown>) => l.type === "recharge")
        .map((l: Record<string, number>) => ({ x: l.x, y: l.y }));

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
