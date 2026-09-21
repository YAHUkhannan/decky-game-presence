const manifest = { name: "Game Presence" };
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error("[@decky/api]: Failed to connect to the loader API.");
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
} catch {
    api = internalAPIConnection.connect(1, manifest.name);
}
const callable = api.callable;
const getStatus = callable("get_status");
const setEnabled = callable("set_enabled");
const updateActivity = callable("update_activity");
const clearActivity = callable("clear_activity");

const e = window.SP_REACT.createElement;
const { useEffect, useState, useCallback } = window.SP_REACT;

function shouldIgnore(name) {
    const n = (name || "").toLowerCase();
    if (!n) return true;
    return n.includes("discord") || n.includes("vesktop") || n.includes("vencord")
        || n === "steam" || n === "steam deck"
        || n.includes("steam linux runtime") || n.startsWith("proton ");
}

function overviewName(appid) {
    try {
        const info = appStore.GetAppOverviewByGameID(String(appid));
        return info?.display_name || "";
    } catch {
        return "";
    }
}

function currentSteamApp() {
    try {
        const running = DFL.Router?.MainRunningApp;
        if (running?.appid) return running.appid;
        const apps = DFL.Router?.RunningApps || [];
        for (const app of apps) {
            const name = overviewName(app.appid);
            if (!shouldIgnore(name)) return app.appid;
        }
    } catch {}
    return null;
}

let holdScript = "";

async function syncSteamShortcut(name) {
    if (!name || !SteamClient?.Apps) return;
    const exe = holdScript;
    if (!exe) return;
    try {
        let data = null;
        if (typeof SteamClient.Apps.GetShortcutDataForPath === "function") {
            data = await SteamClient.Apps.GetShortcutDataForPath(exe);
        }
        const existingId = (data && typeof data === "object")
            ? (data.appid || data.unAppID || data.appid)
            : null;
        if (existingId && typeof SteamClient.Apps.SetShortcutName === "function") {
            await SteamClient.Apps.SetShortcutName(existingId, name);
            return;
        }
        if (typeof SteamClient.Apps.AddShortcut === "function") {
            await SteamClient.Apps.AddShortcut(name, exe, "/", "");
        }
    } catch (err) {
        console.error("Game Presence Steam shortcut", err);
    }
}

async function reportApp(appid) {
    const name = overviewName(appid);
    if (shouldIgnore(name)) return;
    await Promise.all([
        updateActivity({
            details: { name },
            appId: String(appid),
            startTime: Date.now(),
        }),
        syncSteamShortcut(name),
    ]);
}

function Panel() {
    const [status, setStatus] = useState({
        enabled: true,
        connected: false,
        playing: "",
        error: "",
    });
    const [busy, setBusy] = useState(false);

    const refresh = useCallback(async () => {
        try {
            const next = await getStatus();
            if (next) {
                if (next.hold_script) holdScript = next.hold_script;
                setStatus(next);
                if (next.playing) syncSteamShortcut(next.playing);
            }
        } catch {}
    }, []);

    useEffect(() => {
        refresh();
        const id = window.setInterval(refresh, 4000);
        return () => window.clearInterval(id);
    }, [refresh]);

    const onToggle = useCallback(async (value) => {
        setBusy(true);
        try {
            const next = await setEnabled(Boolean(value));
            if (next) setStatus(next);
            if (value) {
                const appid = currentSteamApp();
                if (appid) await reportApp(appid);
            }
        } finally {
            setBusy(false);
            refresh();
        }
    }, [refresh]);

    const onReconnect = useCallback(async () => {
        setBusy(true);
        try {
            await setEnabled(true);
            const appid = currentSteamApp();
            if (appid) await reportApp(appid);
        } finally {
            setBusy(false);
            refresh();
        }
    }, [refresh]);

    const onClear = useCallback(async () => {
        setBusy(true);
        try {
            await clearActivity();
        } finally {
            setBusy(false);
            refresh();
        }
    }, [refresh]);

    const statusText = status.playing
        ? status.playing
        : (status.connected ? "Ready" : "Idle");

    return e(DFL.PanelSection, null,
        e(DFL.PanelSectionRow, null,
            e(DFL.ToggleField, {
                label: "Show activity",
                checked: !!status.enabled,
                disabled: busy,
                onChange: onToggle,
            })
        ),
        e(DFL.PanelSectionRow, null,
            e(DFL.Field, { label: "Now playing" }, statusText)
        ),
        status.error
            ? e(DFL.PanelSectionRow, null,
                e(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: onReconnect }, "Reconnect")
              )
            : null,
        status.playing
            ? e(DFL.PanelSectionRow, null,
                e(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: onClear }, "Clear")
              )
            : null
    );
}

var index = DFL.definePlugin(() => {
    const hooks = [];

    const onLifetime = async (app) => {
        try {
            const enabled = (await getStatus())?.enabled;
            if (!enabled) return;
            const appid = app?.unAppID;
            const name = overviewName(appid);
            if (shouldIgnore(name)) return;
            if (app?.bRunning) {
                await reportApp(appid);
            } else {
                await clearActivity();
            }
        } catch (err) {
            console.error("Game Presence", err);
        }
    };

    try {
        hooks.push(SteamClient.GameSessions.RegisterForAppLifetimeNotifications(onLifetime));
    } catch (err) {
        console.error("Game Presence", err);
    }

    (async () => {
        try {
            const status = await getStatus();
            if (status?.hold_script) holdScript = status.hold_script;
            if (!status?.enabled) return;
            const appid = currentSteamApp();
            if (appid) await reportApp(appid);
        } catch {}
    })();

    return {
        title: e("div", { className: DFL.staticClasses.Title }, "Game Presence"),
        content: e(Panel),
        icon: e("span", { style: { fontSize: "16px", fontWeight: 700 } }, "GP"),
        onDismount() {
            hooks.forEach((hook) => {
                try { hook.unregister(); } catch {}
            });
        },
    };
});

export { index as default };
