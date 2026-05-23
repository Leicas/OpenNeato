import { useEffect, useRef } from "preact/hooks";

// Non-overlapping poll loop with visibility awareness.
// When enabled, calls callback(), waits at least intervalMs after it
// completes, then repeats. Pauses when the tab is hidden and resumes
// immediately on return. Never overlaps requests.
export function usePoll(callback: () => Promise<void>, intervalMs: number, enabled: boolean, initialDelayMs = 0): void {
    const callbackRef = useRef(callback);
    callbackRef.current = callback;

    useEffect(() => {
        if (!enabled) return;

        let active = true;
        let timer: ReturnType<typeof setTimeout>;
        let polling = false;

        const poll = async () => {
            if (document.hidden) return;
            polling = true;
            const start = Date.now();
            try {
                await callbackRef.current();
            } catch {
                // Silently ignore poll errors
            }
            polling = false;
            if (active && !document.hidden) {
                const elapsed = Date.now() - start;
                const delay = Math.max(0, intervalMs - elapsed);
                timer = setTimeout(poll, delay);
            }
        };

        const onVisibilityChange = () => {
            if (!active) return;
            if (document.hidden) {
                clearTimeout(timer);
            } else if (!polling) {
                poll();
            }
        };

        document.addEventListener("visibilitychange", onVisibilityChange);
        if (initialDelayMs > 0) {
            timer = setTimeout(poll, initialDelayMs);
        } else {
            poll();
        }

        return () => {
            active = false;
            clearTimeout(timer);
            document.removeEventListener("visibilitychange", onVisibilityChange);
        };
    }, [intervalMs, enabled, initialDelayMs]);
}
