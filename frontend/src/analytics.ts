function currentUrl(): string {
    return `${location.pathname}${location.search}${location.hash}`;
}

function send(url: string, referrer?: string): void {
    const payload = new URLSearchParams();
    payload.set("s", `${screen.width}x${screen.height}`);
    payload.set("l", navigator.language);
    payload.set("t", document.title);
    payload.set("u", url);
    payload.set("r", referrer || document.referrer);

    if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/collect", payload);
    } else {
        void fetch("/api/collect", { method: "POST", body: payload, keepalive: true });
    }
}

export function startAnalytics(): void {
    let previous = currentUrl();
    send(previous);

    const track = () => {
        const next = currentUrl();
        if (next === previous) return;
        send(next, previous);
        previous = next;
    };

    const pushState = history.pushState.bind(history);
    history.pushState = (...args) => {
        pushState(...args);
        track();
    };

    window.addEventListener("hashchange", track);
    window.addEventListener("popstate", track);
}
