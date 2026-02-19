import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api } from "../api";
import backSvg from "../assets/icons/back.svg?raw";
import boltSvg from "../assets/icons/bolt.svg?raw";
import brushSvg from "../assets/icons/brush.svg?raw";
import sideBrushSvg from "../assets/icons/side-brush.svg?raw";
import sparkleSvg from "../assets/icons/sparkle.svg?raw";
import stopSvg from "../assets/icons/stop.svg?raw";
import vacuumSvg from "../assets/icons/vacuum.svg?raw";
import { BatteryIcon } from "../components/battery-icon";
import { Icon } from "../components/icon";
import type { JoystickValue } from "../components/joystick";
import { Joystick } from "../components/joystick";
import { LidarMap } from "../components/lidar-map";
import { useNavigate } from "../components/router";
import { usePolling } from "../hooks/use-polling";
import type { ChargerData, LidarScan } from "../types";

// Convert joystick X/Y to differential wheel distances (mm)
const MAX_DIST_MM = 300;
const MAX_SPEED_MM_S = 200;

interface WheelCommand {
    left: number;
    right: number;
    speed: number;
}

function joystickToWheels(v: JoystickValue): WheelCommand {
    if (v.magnitude === 0) return { left: 0, right: 0, speed: 0 };

    const speed = Math.round(v.magnitude * MAX_SPEED_MM_S);
    const fwd = v.y * MAX_DIST_MM * v.magnitude;
    const turn = v.x * MAX_DIST_MM * v.magnitude;

    return { left: Math.round(fwd + turn), right: Math.round(fwd - turn), speed };
}

interface ManualViewProps {
    isManual: boolean;
    charger: ChargerData | null;
    brush: boolean;
    vacuum: boolean;
    sideBrush: boolean;
    onToggleBrush: () => void;
    onToggleVacuum: () => void;
    onToggleSideBrush: () => void;
    onToggleAll: () => void;
}

export function ManualView({
    isManual,
    charger,
    brush,
    vacuum,
    sideBrush,
    onToggleBrush,
    onToggleVacuum,
    onToggleSideBrush,
    onToggleAll,
}: ManualViewProps) {
    const navigate = useNavigate();
    const [stopping, setStopping] = useState(false);
    const [mapSize, setMapSize] = useState(280);
    const mapContainerRef = useRef<HTMLDivElement>(null);

    // Poll LIDAR only when in manual mode
    const lidar = usePolling<LidarScan>(api.getLidar, isManual ? 1000 : 0);

    // Measure available map container width
    useEffect(() => {
        const el = mapContainerRef.current;
        if (!el) return;
        const observer = new ResizeObserver((entries) => {
            for (const entry of entries) {
                const w = Math.floor(entry.contentRect.width);
                setMapSize(Math.min(w, 400));
            }
        });
        observer.observe(el);
        return () => observer.disconnect();
    }, []);

    // Send move command (fire-and-forget, throttled)
    const moveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const lastMove = useRef<WheelCommand | null>(null);

    const onJoystickMove = useCallback(
        (v: JoystickValue) => {
            if (!isManual || stopping) return;
            const wheels = joystickToWheels(v);
            lastMove.current = wheels;

            if (moveTimer.current) return;
            moveTimer.current = setTimeout(() => {
                moveTimer.current = null;
                const m = lastMove.current;
                if (m && (m.left !== 0 || m.right !== 0)) {
                    api.manualMove(m.left, m.right, m.speed).catch(() => {});
                }
            }, 200);
        },
        [isManual, stopping],
    );

    const onJoystickRelease = useCallback(() => {
        if (!isManual || stopping) return;
        lastMove.current = null;
        if (moveTimer.current) {
            clearTimeout(moveTimer.current);
            moveTimer.current = null;
        }
        api.manualMove(0, 0, 0).catch(() => {});
    }, [isManual, stopping]);

    // Navigate away only after polled state confirms manual mode ended
    useEffect(() => {
        if (stopping && !isManual) {
            navigate("/");
        }
    }, [stopping, isManual, navigate]);

    const handleStop = useCallback(() => {
        if (stopping) return;
        setStopping(true);
        api.manual(false).catch(() => setStopping(false));
    }, [stopping]);

    return (
        <>
            {/* Header */}
            <div class="header">
                <button
                    type="button"
                    class="header-back-btn"
                    aria-label="Back"
                    disabled={stopping}
                    onClick={() => navigate("/")}
                >
                    <Icon svg={backSvg} />
                </button>
                <h1>Manual</h1>
                <div class="header-right-spacer" />
            </div>

            {/* Status bar */}
            {charger && (
                <div class="manual-status-bar">
                    <div class="manual-status-item">
                        <BatteryIcon pct={charger.fuelPercent} />
                        <span>{charger.fuelPercent}%</span>
                    </div>
                    {(charger.chargingActive || charger.extPwrPresent) && (
                        <div class="manual-status-item">
                            <Icon svg={boltSvg} />
                            <span>{charger.chargingActive ? "Charging" : "Docked"}</span>
                        </div>
                    )}
                </div>
            )}

            <div class="manual-page">
                {/* LIDAR map */}
                <div class="manual-map" ref={mapContainerRef}>
                    <LidarMap scan={lidar.data} size={mapSize} />
                    {!lidar.data && !isManual && <div class="manual-map-error">Not in manual mode</div>}
                    {!lidar.data && isManual && lidar.error && <div class="manual-map-error">LIDAR unavailable</div>}
                </div>

                {/* Controls area */}
                <div class={`manual-controls${isManual && !stopping ? "" : " disabled"}`}>
                    {/* Joystick */}
                    <div class="manual-joystick">
                        <Joystick size={160} onMove={onJoystickMove} onRelease={onJoystickRelease} />
                    </div>

                    {/* Motor toggles */}
                    <div class="manual-motors">
                        <button
                            type="button"
                            class={`manual-motor-btn${brush ? " active" : ""}`}
                            disabled={!isManual || stopping}
                            onClick={onToggleBrush}
                        >
                            <Icon svg={brushSvg} />
                            Brush
                        </button>
                        <button
                            type="button"
                            class={`manual-motor-btn${vacuum ? " active" : ""}`}
                            disabled={!isManual || stopping}
                            onClick={onToggleVacuum}
                        >
                            <Icon svg={vacuumSvg} />
                            Vacuum
                        </button>
                        <button
                            type="button"
                            class={`manual-motor-btn${sideBrush ? " active" : ""}`}
                            disabled={!isManual || stopping}
                            onClick={onToggleSideBrush}
                        >
                            <Icon svg={sideBrushSvg} />
                            Side
                        </button>
                        <button
                            type="button"
                            class={`manual-motor-btn${brush && vacuum && sideBrush ? " active" : ""}`}
                            disabled={!isManual || stopping}
                            onClick={onToggleAll}
                        >
                            <Icon svg={sparkleSvg} />
                            All
                        </button>
                    </div>
                </div>

                {/* Stop button */}
                <div class="manual-stop">
                    <button
                        type="button"
                        class={`action-btn manual-stop-btn${stopping ? " pending" : ""}`}
                        disabled={!isManual || stopping}
                        onClick={handleStop}
                    >
                        <Icon svg={stopSvg} />
                        Stop
                    </button>
                </div>
            </div>
        </>
    );
}
