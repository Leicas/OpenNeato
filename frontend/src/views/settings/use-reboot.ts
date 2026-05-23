import { useCallback, useRef, useState } from "preact/hooks";
import { usePoll } from "../../hooks/use-poll";
import type { SystemData } from "../../types";

export function useReboot(currentUptime: number) {
    const [rebooting, setRebooting] = useState(false);
    const uptimeBeforeReboot = useRef(0);

    usePoll(
        async () => {
            const res = await fetch("/api/system");
            if (!res.ok) throw new Error();
            const data: SystemData = await res.json();
            if (data.uptime < uptimeBeforeReboot.current) {
                window.location.reload();
            }
        },
        2000,
        rebooting,
        2000,
    );

    const startRebootFlow = useCallback(() => {
        uptimeBeforeReboot.current = currentUptime;
        setRebooting(true);
    }, [currentUptime]);

    return { rebooting, startRebootFlow };
}
