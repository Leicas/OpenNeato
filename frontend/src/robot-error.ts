import type { ErrorData } from "./types";

export interface NormalizedRobotError {
    kind: "error" | "warning";
    title: string;
    message: string;
}

export function normalizeRobotError(error: ErrorData | null | undefined): NormalizedRobotError | null {
    if (!error?.hasError) return null;

    const kind = error.kind === "warning" ? "warning" : "error";
    const title = kind === "warning" ? "Robot Notice" : "Robot Attention Needed";
    const message = error.displayMessage || `Robot reported error ${error.errorCode}.`;

    return { kind, title, message };
}
