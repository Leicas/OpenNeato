import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api } from "./api";
import { Route, Router } from "./components/router";
import { usePolling } from "./hooks/use-polling";
import type { ChargerData, ErrorData, FirmwareVersion, StateData, SystemData } from "./types";
import { DashboardView } from "./views/dashboard";
import { LogsView } from "./views/logs";
import { ManualView } from "./views/manual";
import { ScheduleView } from "./views/schedule";
import { SettingsView } from "./views/settings";

type Theme = "system" | "dark" | "light";

const THEME_DARK = "#161618";
const THEME_LIGHT = "#ffffff";

function setThemeColor(color: string) {
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", color);
}

function applyTheme(theme: Theme) {
    const html = document.documentElement;
    html.classList.remove("light", "system-theme");
    if (theme === "light") {
        html.classList.add("light");
        setThemeColor(THEME_LIGHT);
    } else if (theme === "system") {
        html.classList.add("system-theme");
        const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        setThemeColor(prefersDark ? THEME_DARK : THEME_LIGHT);
    } else {
        setThemeColor(THEME_DARK);
    }
}

function loadTheme(): Theme {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark" || saved === "system") return saved;
    return "system";
}

export function App() {
    const [theme, setTheme] = useState<Theme>(loadTheme);

    const themeInitialized = useRef(false);
    useEffect(() => {
        applyTheme(theme);
        if (themeInitialized.current) {
            localStorage.setItem("theme", theme);
        }
        themeInitialized.current = true;

        // When using system theme, track OS preference changes for status bar color
        if (theme === "system") {
            const mq = window.matchMedia("(prefers-color-scheme: dark)");
            const onChange = (e: MediaQueryListEvent) => setThemeColor(e.matches ? THEME_DARK : THEME_LIGHT);
            mq.addEventListener("change", onChange);
            return () => mq.removeEventListener("change", onChange);
        }
    }, [theme]);

    const state = usePolling<StateData>(api.getState, 2000);
    const charger = usePolling<ChargerData>(api.getCharger, 5000);
    const error = usePolling<ErrorData>(api.getError, 2000);
    const system = usePolling<SystemData>(api.getSystem, 10000);
    const firmware = usePolling<FirmwareVersion>(api.getFirmwareVersion, 60000);

    // Derive manual mode from polled state — single source of truth
    const isManual = state.data?.uiState?.includes("MANUALCLEANING") ?? false;

    // Motor toggle state — owned at app level, persists across page navigation
    const [brush, setBrush] = useState(false);
    const [vacuum, setVacuum] = useState(false);
    const [sideBrush, setSideBrush] = useState(false);

    // Reset motor state when manual mode ends
    useEffect(() => {
        if (!isManual) {
            setBrush(false);
            setVacuum(false);
            setSideBrush(false);
        }
    }, [isManual]);

    const sendMotors = useCallback((b: boolean, v: boolean, s: boolean) => {
        api.manualMotors(b, v, s).catch(() => {});
    }, []);

    const toggleBrush = useCallback(() => {
        const next = !brush;
        setBrush(next);
        sendMotors(next, vacuum, sideBrush);
    }, [brush, vacuum, sideBrush, sendMotors]);

    const toggleVacuum = useCallback(() => {
        const next = !vacuum;
        setVacuum(next);
        sendMotors(brush, next, sideBrush);
    }, [brush, vacuum, sideBrush, sendMotors]);

    const toggleSideBrush = useCallback(() => {
        const next = !sideBrush;
        setSideBrush(next);
        sendMotors(brush, vacuum, next);
    }, [brush, vacuum, sideBrush, sendMotors]);

    const toggleAll = useCallback(() => {
        const allOn = brush && vacuum && sideBrush;
        const next = !allOn;
        setBrush(next);
        setVacuum(next);
        setSideBrush(next);
        sendMotors(next, next, next);
    }, [brush, vacuum, sideBrush, sendMotors]);

    return (
        <Router>
            <Route path="/">
                <DashboardView
                    system={system}
                    firmware={firmware}
                    error={error}
                    state={state}
                    charger={charger}
                    isManual={isManual}
                />
            </Route>
            <Route path="/settings">
                <SettingsView theme={theme} onThemeChange={setTheme} system={system.data} firmware={firmware.data} />
            </Route>
            <Route path="/manual">
                <ManualView
                    isManual={isManual}
                    charger={charger.data}
                    brush={brush}
                    vacuum={vacuum}
                    sideBrush={sideBrush}
                    onToggleBrush={toggleBrush}
                    onToggleVacuum={toggleVacuum}
                    onToggleSideBrush={toggleSideBrush}
                    onToggleAll={toggleAll}
                />
            </Route>
            <Route path="/schedule">
                <ScheduleView />
            </Route>
            <Route path="/logs" prefix>
                <LogsView />
            </Route>
        </Router>
    );
}
