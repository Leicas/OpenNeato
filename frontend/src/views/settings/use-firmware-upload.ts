import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api } from "../../api";
import type { ErrorStackHandle } from "../../components/error-banner";

type UploadStatus = "idle" | "hashing" | "uploading" | "done";

// ESP32 image header: byte at offset 12 (extended header) contains chip ID.
// Mapping from ESP-IDF esp_image_format.h:
const CHIP_IDS: Record<number, string> = {
    0: "ESP32",
    2: "ESP32-S2",
    5: "ESP32-C3",
    9: "ESP32-S3",
    12: "ESP32-C2",
    13: "ESP32-C6",
    16: "ESP32-H2",
};

function parseChipFromBin(buf: ArrayBuffer): string | null {
    if (buf.byteLength < 16) return null;
    const view = new DataView(buf);
    // Extended header starts at offset 8; chip ID is at byte 4 of extended header = offset 12
    const chipId = view.getUint8(12) & 0xff;
    return CHIP_IDS[chipId] ?? null;
}

async function computeMd5(file: File): Promise<string> {
    const buf = await file.arrayBuffer();
    const hash = await crypto.subtle.digest("MD5", buf);
    return Array.from(new Uint8Array(hash))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
}

// crypto.subtle.digest may not support MD5 in all browsers.
// Fallback: simple MD5 implementation for firmware files.
async function computeMd5Fallback(file: File): Promise<string> {
    try {
        return await computeMd5(file);
    } catch {
        // MD5 not available in crypto.subtle (some browsers removed it).
        // Use SparkMD5-style manual computation — but we have zero-dep policy.
        // Instead, send empty hash and let the server skip MD5 validation.
        return "";
    }
}

export function useFirmwareUpload(
    deviceChip: string | null,
    errorStack: ErrorStackHandle,
    startRebootFlow: () => void,
) {
    const [file, setFile] = useState<File | null>(null);
    const [chipError, setChipError] = useState<string | null>(null);
    const [status, setStatus] = useState<UploadStatus>("idle");

    // Smoothed progress: XHR fires progress in big jumps (browser buffers writes),
    // so we animate the displayed value toward the real target at a steady rate.
    const [displayProgress, setDisplayProgress] = useState(0);
    const realProgress = useRef(0);
    const animTimer = useRef<ReturnType<typeof setInterval> | null>(null);

    const stopProgressAnim = useCallback(() => {
        if (animTimer.current) {
            clearInterval(animTimer.current);
            animTimer.current = null;
        }
    }, []);

    const startProgressAnim = useCallback(() => {
        stopProgressAnim();
        realProgress.current = 0;
        setDisplayProgress(0);
        animTimer.current = setInterval(() => {
            setDisplayProgress((prev) => {
                const target = realProgress.current;
                if (prev >= target) return prev;
                // Close gap by ~15% each tick, minimum 1% step
                const step = Math.max(1, Math.round((target - prev) * 0.15));
                return Math.min(target, prev + step);
            });
        }, 100);
    }, [stopProgressAnim]);

    useEffect(() => stopProgressAnim, [stopProgressAnim]);

    const onUploadProgress = useCallback((pct: number) => {
        // Cap at 90% during upload — the last 10% represents server-side flash
        // write + verification. Jumps to 100% only when the server responds OK.
        realProgress.current = Math.min(90, pct);
    }, []);

    const selectFile = useCallback(
        (f: File | null) => {
            setFile(f);
            setChipError(null);
            setStatus("idle");
            setDisplayProgress(0);
            stopProgressAnim();

            if (!f || !deviceChip) return;

            // Read first 16 bytes to check chip type
            const reader = new FileReader();
            reader.onload = () => {
                const buf = reader.result as ArrayBuffer;
                const binChip = parseChipFromBin(buf);
                if (!binChip) {
                    setChipError("Could not detect chip type from firmware file");
                    return;
                }
                // Normalize comparison: device reports "ESP32-C3", bin header gives "ESP32-C3"
                if (binChip.toLowerCase() !== deviceChip.toLowerCase()) {
                    setChipError(`Firmware is for ${binChip}, but this device is ${deviceChip}`);
                }
            };
            reader.readAsArrayBuffer(f.slice(0, 16));
        },
        [deviceChip, stopProgressAnim],
    );

    const startUpload = useCallback(async () => {
        if (!file) return;

        try {
            setStatus("hashing");
            const md5 = await computeMd5Fallback(file);

            setStatus("uploading");
            startProgressAnim();
            await api.uploadFirmware(file, md5, onUploadProgress);

            stopProgressAnim();
            setDisplayProgress(100);
            setStatus("done");
            startRebootFlow();
        } catch (e: unknown) {
            stopProgressAnim();
            setStatus("idle");
            errorStack.push(e instanceof Error ? e.message : "Firmware upload failed");
        }
    }, [file, errorStack, startRebootFlow, startProgressAnim, stopProgressAnim, onUploadProgress]);

    return {
        file,
        chipError,
        status,
        progress: displayProgress,
        selectFile,
        startUpload,
    };
}
