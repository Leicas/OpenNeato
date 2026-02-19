import { useEffect, useRef } from "preact/hooks";
import type { LidarScan } from "../types";

interface LidarMapProps {
    scan: LidarScan | null;
    size: number;
}

const MAX_RANGE_MM = 5000;
const POINT_COLOR = "#3b9eff";

export function LidarMap({ scan, size }: LidarMapProps) {
    const canvasRef = useRef<HTMLCanvasElement>(null);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const dpr = window.devicePixelRatio || 1;
        canvas.width = size * dpr;
        canvas.height = size * dpr;
        canvas.style.width = `${size}px`;
        canvas.style.height = `${size}px`;

        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.scale(dpr, dpr);

        const cx = size / 2;
        const cy = size / 2;
        const scale = (size / 2 - 8) / MAX_RANGE_MM;

        // Read theme colors from CSS variables
        const styles = getComputedStyle(document.documentElement);
        const bgColor = styles.getPropertyValue("--surface").trim() || "#1a1a1c";
        const gridColor = styles.getPropertyValue("--border").trim() || "rgba(255, 255, 255, 0.06)";
        const robotColor = styles.getPropertyValue("--text-dim").trim() || "#8a8a8e";

        // Background
        ctx.fillStyle = bgColor;
        ctx.fillRect(0, 0, size, size);

        // Grid rings (1m intervals)
        ctx.strokeStyle = gridColor;
        ctx.lineWidth = 1;
        for (let r = 1000; r <= MAX_RANGE_MM; r += 1000) {
            ctx.beginPath();
            ctx.arc(cx, cy, r * scale, 0, Math.PI * 2);
            ctx.stroke();
        }

        // Crosshair
        ctx.beginPath();
        ctx.moveTo(cx, 4);
        ctx.lineTo(cx, size - 4);
        ctx.moveTo(4, cy);
        ctx.lineTo(size - 4, cy);
        ctx.stroke();

        // LIDAR points
        if (scan) {
            ctx.fillStyle = POINT_COLOR;
            for (const p of scan.points) {
                if (p.error !== 0 || p.dist === 0) continue;
                // Neato LIDAR: 0deg = right, CCW positive
                // Canvas: 0deg = up (forward), CW positive
                // Rotate 90deg CW: canvasAngle = -(angle - 90) = 90 - angle
                const rad = ((90 - p.angle) * Math.PI) / 180;
                const px = cx + p.dist * scale * Math.cos(rad);
                const py = cy - p.dist * scale * Math.sin(rad);
                ctx.beginPath();
                ctx.arc(px, py, 1.5, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        // Robot indicator (center triangle pointing up = forward)
        ctx.fillStyle = robotColor;
        ctx.beginPath();
        ctx.moveTo(cx, cy - 8);
        ctx.lineTo(cx - 5, cy + 4);
        ctx.lineTo(cx + 5, cy + 4);
        ctx.closePath();
        ctx.fill();
    }, [scan, size]);

    return <canvas ref={canvasRef} class="lidar-canvas" />;
}
