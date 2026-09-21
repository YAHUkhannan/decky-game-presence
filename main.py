import asyncio
import json
import os
import pwd
import re
import signal
import socket
import struct
import subprocess
import time
import urllib.request
import uuid

import decky
from settings import SettingsManager

FALLBACK_CLIENT_ID = "1055680235682672682"
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
DISPLAY_NUM = ":99"
MAX_START_FAILS = 2
DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"

USER = os.environ.get("DECKY_USER") or pwd.getpwuid(int(os.environ.get("DECKY_USER_ID", "1000"))).pw_name
UID = int(os.environ.get("DECKY_USER_ID") or pwd.getpwnam(USER).pw_uid)
GID = pwd.getpwnam(USER).pw_gid
HOME = os.environ.get("DECKY_USER_HOME") or pwd.getpwnam(USER).pw_dir
RUNTIME = f"/run/user/{UID}"
STATE_DIR = os.environ.get("DECKY_PLUGIN_RUNTIME_DIR") or os.path.join(HOME, ".local/share/game-presence")
OWNED_FLAG = os.path.join(STATE_DIR, "owned")
HOLD_SCRIPT = os.path.join(STATE_DIR, "steam-hold.sh")
XVFB_PID_FILE = os.path.join(STATE_DIR, "xvfb.pid")
DISCORD_PID_FILE = os.path.join(STATE_DIR, "discord.pid")
DETECTABLE_CACHE = os.path.join(STATE_DIR, "detectable.json")
INDEX_CACHE = os.path.join(STATE_DIR, "index.json")
STEAMAPPS = os.path.join(HOME, ".local/share/Steam/steamapps")
IGNORE_APPIDS = {"0", "7", "753", "228980", "1391110", "1493710", "1628350"}
APPID_RE = re.compile(r"(?:SteamLaunch\s+)?AppId[=:](\d+)", re.I)
STEAMAPP_RE = re.compile(r"Steam(?:App|Game)Id=(\d+)")

settings = SettingsManager(
    name="settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)
settings.read()


def log(msg, *args):
    decky.logger.info(msg, *args)


def _user_env():
    env = os.environ.copy()
    env.update(
        {
            "HOME": HOME,
            "USER": USER,
            "LOGNAME": USER,
            "XDG_RUNTIME_DIR": RUNTIME,
            "XDG_CONFIG_HOME": os.path.join(HOME, ".config"),
            "XDG_CACHE_HOME": os.path.join(HOME, ".cache"),
            "XDG_DATA_HOME": os.path.join(HOME, ".local/share"),
            "DISPLAY": DISPLAY_NUM,
            "GDK_BACKEND": "x11",
            "OZONE_PLATFORM": "x11",
        }
    )
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("SWAYSOCK", None)
    return env


def _drop_privs():
    os.nice(15)
    os.setgid(GID)
    os.setuid(UID)
    os.chdir(HOME)


def _spawn(cmd):
    os.makedirs(STATE_DIR, exist_ok=True)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_user_env(),
        start_new_session=True,
        preexec_fn=_drop_privs,
    )


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid(path, pid):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _kill_pid(pid, sig=signal.SIGTERM):
    if not _pid_alive(pid):
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def ipc_paths():
    roots = [
        os.path.join(RUNTIME, "app/com.discordapp.Discord"),
        RUNTIME,
        "/tmp",
    ]
    paths = []
    for root in roots:
        for i in range(10):
            paths.append(os.path.join(root, f"discord-ipc-{i}"))
    return paths


def find_ipc():
    for path in ipc_paths():
        if os.path.exists(path):
            return path
    return None


def ipc_connectable():
    path = find_ipc()
    if not path:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def ipc_ready():
    rpc = Rpc(FALLBACK_CLIENT_ID)
    try:
        rpc.connect()
        return True
    except Exception:
        return False
    finally:
        rpc.close()


def discord_running():
    try:
        return subprocess.call(
            ["pgrep", "-u", str(UID), "-x", "Discord"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) == 0
    except OSError:
        return False


def we_own_stack():
    return os.path.exists(OWNED_FLAG)


def clear_stale_singleton():
    if discord_running():
        return
    cfg = os.path.join(HOME, ".config/discord")
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(cfg, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
    for path in ipc_paths():
        try:
            if os.path.exists(path) and not ipc_connectable():
                os.remove(path)
        except OSError:
            pass


def start_hidden_discord():
    if discord_running() or we_own_stack():
        return None
    os.makedirs(STATE_DIR, exist_ok=True)
    clear_stale_singleton()

    xvfb = _spawn(["Xvfb", DISPLAY_NUM, "-screen", "0", "800x480x24", "-nolisten", "tcp"])
    _write_pid(XVFB_PID_FILE, xvfb.pid)
    time.sleep(1.0)

    discord_bin = os.path.join(HOME, ".config/discord/Discord")
    if not os.access(discord_bin, os.X_OK):
        discord_bin = "/usr/bin/discord"

    discord = _spawn(
        [
            discord_bin,
            "--start-minimized",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--renderer-process-limit=1",
            "--enable-low-end-device-mode",
            "--ozone-platform=x11",
        ]
    )
    _write_pid(DISCORD_PID_FILE, discord.pid)
    with open(OWNED_FLAG, "w", encoding="utf-8") as f:
        f.write("1")
    return discord.pid


def stop_hidden_discord():
    if not we_own_stack():
        return
    for path in (DISCORD_PID_FILE, XVFB_PID_FILE):
        _kill_pid(_read_pid(path))
    if discord_running() and we_own_stack():
        try:
            subprocess.call(
                ["pkill", "-u", str(UID), "-x", "Discord"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
    _kill_pid(_read_pid(XVFB_PID_FILE))
    for path in (OWNED_FLAG, XVFB_PID_FILE, DISCORD_PID_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


class Rpc:
    def __init__(self, app_id):
        self.app_id = app_id
        self.sock = None

    def connect(self):
        path = find_ipc()
        if not path:
            raise RuntimeError("Discord IPC socket not found")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(4)
        self.sock.connect(path)
        self._send({"v": 1, "client_id": self.app_id}, OP_HANDSHAKE)
        data = self._recv()
        if not data or data.get("evt") != "READY":
            raise RuntimeError(f"Discord handshake failed: {data}")

    def set_activity(self, activity):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }
        self._send(payload, OP_FRAME)
        data = self._recv()
        if data.get("evt") == "ERROR":
            raise RuntimeError(data.get("data") or data)
        return data

    def clear(self):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid()},
            "nonce": str(uuid.uuid4()),
        }
        self._send(payload, OP_FRAME)
        try:
            self._recv()
        except Exception:
            pass

    def close(self, clear=False):
        if not self.sock:
            return
        if clear:
            try:
                self.clear()
            except Exception:
                pass
            try:
                self._send({}, OP_CLOSE)
            except Exception:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    def alive(self):
        if not self.sock:
            return False
        try:
            self._send({}, OP_PING)
            self._recv()
            return True
        except Exception:
            return False

    def _send(self, payload, op):
        raw = json.dumps(payload).encode("utf-8")
        self.sock.sendall(struct.pack("<ii", op, len(raw)) + raw)

    def _recv(self):
        header = self._read_exact(8)
        _op, length = struct.unpack("<ii", header)
        body = self._read_exact(length)
        return json.loads(body.decode("utf-8"))

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise TimeoutError("Discord IPC closed")
            buf += chunk
        return buf


_INDEX = None


def _compact_app(app):
    return {
        "id": str(app.get("id") or ""),
        "name": app.get("name") or "",
        "icon_hash": app.get("icon_hash") or app.get("cover_image_hash") or "",
    }


def _build_index(apps):
    by_steam = {}
    by_name = {}
    by_exe = {}
    for app in apps:
        rec = _compact_app(app)
        if not rec["id"]:
            continue
        for sku in app.get("third_party_skus") or []:
            if sku.get("distributor") == "steam" and sku.get("id"):
                by_steam[str(sku.get("id"))] = rec
        n = _norm(rec["name"])
        if n:
            by_name[n] = rec
        for exe in app.get("executables") or []:
            base = os.path.basename(str(exe.get("name") or "")).lower()
            if base.endswith(".exe") or base.endswith(".so") or "." not in base:
                if len(base) >= 4:
                    by_exe[base] = rec
    return {"by_steam": by_steam, "by_name": by_name, "by_exe": by_exe}


def load_detectable():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    os.makedirs(STATE_DIR, exist_ok=True)
    now = time.time()
    try:
        with open(INDEX_CACHE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if now - float(cached.get("fetched", 0)) < 86400 and cached.get("by_steam"):
            _INDEX = cached
            return _INDEX
    except Exception:
        pass
    apps = None
    try:
        with open(DETECTABLE_CACHE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        apps = blob.get("apps") if isinstance(blob, dict) else blob
    except Exception:
        apps = None
    if not apps:
        req = urllib.request.Request(
            DETECTABLE_URL,
            headers={"User-Agent": "GamePresence/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            apps = json.loads(resp.read().decode("utf-8"))
        with open(DETECTABLE_CACHE, "w", encoding="utf-8") as f:
            json.dump({"fetched": now, "apps": apps}, f)
    index = _build_index(apps)
    index["fetched"] = now
    with open(INDEX_CACHE, "w", encoding="utf-8") as f:
        json.dump(index, f)
    _INDEX = index
    return _INDEX


def _norm(text):
    return "".join(ch.lower() for ch in (text or "") if ch.isalnum())


def match_discord_app(name, steam_appid):
    index = load_detectable()
    steam_id = str(steam_appid or "").strip()
    if steam_id and steam_id.isdigit() and steam_id not in IGNORE_APPIDS:
        hit = (index.get("by_steam") or {}).get(steam_id)
        if hit:
            return hit
    needle = _norm(name)
    if needle and len(needle) >= 6:
        hit = (index.get("by_name") or {}).get(needle)
        if hit:
            return hit
    return None


def steamapps_roots():
    roots = [STEAMAPPS]
    vdf = os.path.join(os.path.dirname(STEAMAPPS), "steamapps", "libraryfolders.vdf")
    alt = os.path.join(HOME, ".local/share/Steam/steamapps/libraryfolders.vdf")
    for path in (vdf, alt):
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            roots.append(os.path.join(m.group(1), "steamapps"))
    seen = []
    for root in roots:
        if root not in seen and os.path.isdir(root):
            seen.append(root)
    return seen


def name_for_appid(appid):
    appid = str(appid)
    index = load_detectable()
    hit = (index.get("by_steam") or {}).get(appid)
    if hit and hit.get("name"):
        return hit["name"]
    for root in steamapps_roots():
        acf = os.path.join(root, f"appmanifest_{appid}.acf")
        if not os.path.isfile(acf):
            continue
        try:
            text = open(acf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r'"name"\s+"([^"]+)"', text)
        if m:
            return m.group(1)
    return None


def scan_running_appid():
    found = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        text = ""
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read()
            text += raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        try:
            env = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", "replace")
            text += " " + env
        except OSError:
            pass
        for rx in (APPID_RE, STEAMAPP_RE):
            for m in rx.finditer(text):
                appid = m.group(1)
                if appid not in IGNORE_APPIDS and appid not in found:
                    found.append(appid)
    for appid in found:
        if name_for_appid(appid):
            return appid
    return found[0] if found else None


def public_error(err):
    if not err:
        return ""
    return "Couldn't update Discord. Try Reconnect."


def write_hold_script():
    os.makedirs(STATE_DIR, exist_ok=True)
    body = "#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile :; do sleep 3600; done\n"
    try:
        with open(HOLD_SCRIPT, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(HOLD_SCRIPT, 0o755)
    except OSError as exc:
        log("Could not write Steam hold script: %s", exc)


def steam_header_url(steam_appid):
    steam_id = str(steam_appid or "").strip()
    if steam_id.isdigit() and steam_id not in IGNORE_APPIDS:
        return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{steam_id}/header.jpg"
    return None


def build_activity(name, steam_appid, start, discord_app):
    activity = {
        "type": 0,
        "name": name,
        "timestamps": {"start": start},
    }
    if discord_app:
        icon = discord_app.get("icon_hash") or discord_app.get("cover_image_hash")
        if icon:
            activity["assets"] = {
                "large_image": f"https://cdn.discordapp.com/app-icons/{discord_app['id']}/{icon}.png",
                "large_text": discord_app.get("name") or name,
            }
        return activity, str(discord_app["id"])

    image = steam_header_url(steam_appid)
    if image:
        activity["assets"] = {"large_image": image, "large_text": name}
    activity["details"] = name
    activity["state"] = "on Steam Deck"
    return activity, FALLBACK_CLIENT_ID


class Plugin:
    def __init__(self):
        self._fails = 0
        self._last_error = ""
        self._current_game = ""
        self._desired = None
        self._loop_task = None
        self._rpc = None
        self._rpc_client_id = None
        self._misses = 0
        self._from_scan = False

    async def _main(self):
        self._fails = 0
        self._last_error = ""
        self._current_game = ""
        self._desired = None
        self._rpc = None
        self._rpc_client_id = None
        self._misses = 0
        self._from_scan = False
        os.makedirs(STATE_DIR, exist_ok=True)
        write_hold_script()
        log("Game Presence started")
        asyncio.create_task(asyncio.to_thread(load_detectable))
        self._loop_task = asyncio.create_task(self._keepalive())

    async def _unload(self):
        self._desired = None
        if self._loop_task:
            self._loop_task.cancel()
        await self._drop_rpc(clear=True)
        await asyncio.to_thread(stop_hidden_discord)

    async def _drop_rpc(self, clear=False):
        rpc = self._rpc
        self._rpc = None
        self._rpc_client_id = None
        if rpc:
            await asyncio.to_thread(rpc.close, clear)

    async def _keepalive(self):
        while True:
            try:
                await asyncio.sleep(8)
                if not bool(settings.getSetting("enabled", True)):
                    continue
                scanned = await asyncio.to_thread(scan_running_appid)
                if scanned:
                    self._misses = 0
                    if self._desired and not self._from_scan:
                        pass
                    else:
                        name = await asyncio.to_thread(name_for_appid, scanned) or self._current_game
                        if name and (not self._desired or str(self._desired.get("appId")) != str(scanned)):
                            self._from_scan = True
                            self._desired = {
                                "name": name,
                                "appId": str(scanned),
                                "start": int(time.time()),
                            }
                            self._fails = 0
                            await self._apply(self._desired)
                elif self._desired and self._from_scan:
                    self._misses += 1
                    if self._misses >= 3:
                        await self.clear_activity()
                if self._desired and not (self._rpc and self._rpc.sock):
                    await self._apply(self._desired, quiet=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log("keepalive error: %s", exc)

    async def get_status(self):
        connected = bool(self._rpc and self._rpc.sock) or ipc_connectable()
        return {
            "enabled": bool(settings.getSetting("enabled", True)),
            "connected": connected,
            "playing": self._current_game,
            "error": public_error(self._last_error),
            "hold_script": HOLD_SCRIPT,
        }

    async def set_enabled(self, enabled: bool):
        settings.setSetting("enabled", bool(enabled))
        try:
            settings.commit()
        except Exception:
            pass
        if not enabled:
            self._fails = 0
            self._desired = None
            self._current_game = ""
            await self._drop_rpc(clear=True)
            await asyncio.to_thread(stop_hidden_discord)
        return await self.get_status()

    async def ensure_discord(self):
        if not bool(settings.getSetting("enabled", True)):
            return False
        if self._rpc and self._rpc.sock:
            self._fails = 0
            return True
        if discord_running() and ipc_connectable():
            self._fails = 0
            return True
        if we_own_stack() and discord_running():
            for _ in range(15):
                if ipc_connectable():
                    self._fails = 0
                    return True
                await asyncio.sleep(1)
            return False
        if self._fails >= MAX_START_FAILS:
            self._last_error = "Couldn't connect. Use Reconnect."
            return False
        if discord_running() and not we_own_stack():
            for _ in range(8):
                if ipc_connectable():
                    return True
                await asyncio.sleep(1)
            log("Discord process is up but IPC is dead; starting hidden copy")

        log("Starting Discord")
        try:
            await asyncio.to_thread(start_hidden_discord)
        except Exception as exc:
            self._fails += 1
            self._last_error = str(exc)
            log("Failed to start Discord: %s", exc)
            await asyncio.to_thread(stop_hidden_discord)
            return False

        for _ in range(40):
            if await asyncio.to_thread(ipc_ready):
                self._fails = 0
                self._last_error = ""
                log("Discord IPC is live")
                return True
            await asyncio.sleep(1)

        self._fails += 1
        self._last_error = "Couldn't connect. Use Reconnect."
        log(self._last_error)
        await asyncio.to_thread(stop_hidden_discord)
        return False

    async def _apply(self, desired, quiet=False):
        name = desired["name"]
        steam_appid = desired.get("appId")
        start = desired.get("start") or int(time.time())
        self._current_game = name

        if not await self.ensure_discord():
            return False

        try:
            discord_app = await asyncio.to_thread(match_discord_app, name, steam_appid)
        except Exception as exc:
            log("detectable lookup failed: %s", exc)
            discord_app = None

        activity, client_id = build_activity(name, steam_appid, start, discord_app)
        try:
            rpc = self._rpc
            if rpc is None or self._rpc_client_id != client_id or not rpc.sock:
                if rpc:
                    await asyncio.to_thread(rpc.close, False)
                rpc = Rpc(client_id)
                await asyncio.to_thread(rpc.connect)
                self._rpc = rpc
                self._rpc_client_id = client_id
            data = await asyncio.to_thread(rpc.set_activity, activity)
            shown = (data.get("data") or {}).get("name") or name
            self._last_error = ""
            if not quiet:
                log("Set presence as %s (%s)", shown, client_id)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            await self._drop_rpc(clear=False)
            if not quiet:
                log("Failed to set presence: %s", exc)
            return False

    async def update_activity(self, activity=None, **kwargs):
        if not isinstance(activity, dict):
            activity = {}
        if kwargs:
            activity = {**activity, **kwargs}
        details = activity.get("details") or {}
        if isinstance(details, dict):
            name = details.get("name") or activity.get("name") or ""
        else:
            name = str(details) if details else (activity.get("name") or "")
        name = (name or "").strip()
        if not name:
            return False

        start = activity.get("startTime")
        try:
            start = int(start)
            if start > 10_000_000_000:
                start = start // 1000
        except (TypeError, ValueError):
            start = int(time.time())

        desired = {
            "name": name,
            "appId": str(activity.get("appId") or activity.get("appid") or ""),
            "start": start,
        }
        self._desired = desired
        self._from_scan = False
        self._fails = 0

        ok = False
        for attempt in range(15):
            ok = await self._apply(desired)
            if ok:
                break
            await asyncio.sleep(2)
        return ok

    async def test_presence(self):
        if self._desired:
            return await self._apply(self._desired)
        return await self.update_activity({"details": {"name": "Steam Deck"}, "appId": "0"})

    async def clear_activity(self):
        self._desired = None
        self._current_game = ""
        await self._drop_rpc(clear=True)
        if we_own_stack():
            await asyncio.to_thread(stop_hidden_discord)
        return True
