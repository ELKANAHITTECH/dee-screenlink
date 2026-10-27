#!/usr/bin/env python3
"""
ScreenLink Server  (Windows 10 / 11)

Streams one of your monitors to the ScreenLink Android app as a JPEG stream
over a WebSocket, and injects the phone's touch / keyboard input back into
Windows (mouse, scroll, typing, shortcuts).

    pip install -r requirements.txt
    python screenlink_server.py

The console shows the PIN and the IP address(es) to enter on the phone.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import hmac
import io
import json
import logging
import secrets
import socket
import sys
import threading
import time
import zlib
from dataclasses import dataclass

import mss
from PIL import Image, ImageDraw
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

IS_WINDOWS = sys.platform == "win32"

WS_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = b"SCREENLINK_DISCOVER"
MAX_CLIENTS = 3           # simultaneous phones
MAX_INFLIGHT = 2          # frames sent but not yet acknowledged by the phone
LOCKOUT_FAILS = 5
LOCKOUT_SECONDS = 60

log = logging.getLogger("screenlink")

# --------------------------------------------------------------------------
# Windows plumbing (ctypes, no extra dependencies)
# --------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    # Make sure we work in real pixels on high-DPI displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()

    user32 = ctypes.windll.user32
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
    MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
    MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
    MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004

    def cursor_pos() -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    SDC_TOPOLOGY_CLONE, SDC_TOPOLOGY_EXTEND, SDC_APPLY = 0x2, 0x4, 0x80
    user32.SetDisplayConfig.argtypes = (wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
                                        ctypes.c_void_p, wintypes.UINT)
    user32.SetDisplayConfig.restype = ctypes.c_long

    def set_topology(mode: str) -> bool:
        """Ask Windows to mirror ('clone') or extend the desktop."""
        flag = SDC_TOPOLOGY_EXTEND if mode == "extend" else SDC_TOPOLOGY_CLONE
        return user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | flag) == 0
else:
    def cursor_pos() -> tuple[int, int]:
        return -1, -1

    def set_topology(mode: str) -> bool:
        return False

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "capslock": 0x14, "esc": 0x1B, "space": 0x20, "pageup": 0x21,
    "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D,
    "delete": 0x2E, "win": 0x5B,
}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


def _num(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi, default):
    return max(lo, min(hi, _num(v, default)))


class Injector:
    """Turns phone input messages into real Windows mouse / keyboard input."""

    def __init__(self, grabber: "Grabber"):
        self.grabber = grabber
        self.held: set[str] = set()

    # ---- low level -------------------------------------------------------
    def _mouse(self, flags, data=0, dx=0, dy=0):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _key(self, vk=0, scan=0, flags=0):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    # ---- mouse -----------------------------------------------------------
    def move(self, nx: float, ny: float):
        mon = self.grabber.mon
        if not mon:
            return
        px = mon["left"] + nx * (mon["width"] - 1)
        py = mon["top"] + ny * (mon["height"] - 1)
        vl, vt = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        ax = int((px - vl) * 65535 / max(vw - 1, 1))
        ay = int((py - vt) * 65535 / max(vh - 1, 1))
        self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx=ax, dy=ay)

    def button(self, name: str, down: bool):
        flags = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }.get(name)
        if not flags:
            return
        self._mouse(flags[0] if down else flags[1])
        (self.held.add if down else self.held.discard)(name)

    def wheel(self, dy: int, dx: int):
        if dy:
            self._mouse(MOUSEEVENTF_WHEEL, data=dy)
        if dx:
            self._mouse(MOUSEEVENTF_HWHEEL, data=dx)

    def release_all(self):
        for name in list(self.held):
            self.button(name, False)

    # ---- keyboard --------------------------------------------------------
    def vk_event(self, vk: int, down: bool):
        flags = 0 if down else KEYEVENTF_KEYUP
        if vk in EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        self._key(vk, user32.MapVirtualKeyW(vk, 0), flags)

    def tap(self, vk: int):
        self.vk_event(vk, True)
        self.vk_event(vk, False)

    def type_text(self, text: str):
        for ch in text[:2000]:
            if ch == "\r":
                continue
            if ch == "\n":
                self.tap(VK["enter"])
            elif ch == "\t":
                self.tap(VK["tab"])
            else:
                data = ch.encode("utf-16-le")
                for i in range(0, len(data), 2):
                    unit = int.from_bytes(data[i:i + 2], "little")
                    self._key(0, unit, KEYEVENTF_UNICODE)
                    self._key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)

    def press(self, keys, repeat: int = 1):
        codes = []
        for k in keys[:6]:
            name = str(k).lower()
            if name in VK:
                codes.append(VK[name])
            elif len(name) == 1 and name.isalnum():
                codes.append(ord(name.upper()))
        if not codes:
            return
        for _ in range(max(1, min(repeat, 100))):
            for vk in codes:
                self.vk_event(vk, True)
            for vk in reversed(codes):
                self.vk_event(vk, False)

    # ---- dispatcher ------------------------------------------------------
    def handle(self, m: dict):
        t = m.get("type")
        if t == "mouse":
            action = m.get("action")
            x = _clamp(m.get("x"), 0.0, 1.0, 0.0)
            y = _clamp(m.get("y"), 0.0, 1.0, 0.0)
            btn = str(m.get("button", "left"))
            if action == "move":
                self.move(x, y)
            elif action == "down":
                self.move(x, y)
                self.button(btn, True)
            elif action == "up":
                self.move(x, y)
                self.button(btn, False)
            elif action == "click":
                self.move(x, y)
                self.button(btn, True)
                self.button(btn, False)
            elif action == "scroll":
                self.wheel(int(_clamp(m.get("dy"), -2400, 2400, 0)),
                           int(_clamp(m.get("dx"), -2400, 2400, 0)))
        elif t == "text":
            self.type_text(str(m.get("text", "")))
        elif t == "key":
            keys = m.get("keys")
            if isinstance(keys, list):
                self.press(keys, int(_clamp(m.get("repeat"), 1, 100, 1)))


# --------------------------------------------------------------------------
# Screen capture + JPEG encoding (always called from ONE worker thread,
# because mss handles are bound to the thread that created them)
# --------------------------------------------------------------------------
class Grabber:
    def __init__(self):
        self.sct: mss.base.MSSBase | None = None
        self.mon: dict | None = None
        self.mon_id: int | None = None
        self.last_sig = None

    def _ensure(self):
        if self.sct is None:
            # newer mss renamed mss.mss() to mss.MSS(); support both
            self.sct = (getattr(mss, "MSS", None) or mss.mss)()

    def monitor_list(self) -> list[dict]:
        self._ensure()
        return [
            {"id": i, "width": m["width"], "height": m["height"],
             "left": m["left"], "top": m["top"]}
            for i, m in enumerate(self.sct.monitors) if i >= 1
        ]

    def select(self, idx: int) -> dict:
        self._ensure()
        mons = self.sct.monitors
        if idx < 1 or idx >= len(mons):
            idx = 1
        self.mon_id = idx
        self.mon = dict(mons[idx])
        self.last_sig = None
        return {"id": idx, "width": self.mon["width"], "height": self.mon["height"],
                "left": self.mon["left"], "top": self.mon["top"]}

    def refresh(self):
        """Re-read the monitor layout (after Windows changed mirror/extend)."""
        self.close()
        self.mon = None
        self.mon_id = None
        self.last_sig = None
        self._ensure()

    def close(self):
        if self.sct is not None:
            with contextlib.suppress(Exception):
                self.sct.close()
            self.sct = None

    def grab(self, idx: int, scale: float, quality: int, force: bool = False):
        """Return JPEG bytes, or None if nothing changed since the last frame."""
        self._ensure()
        if self.mon is None or idx != self.mon_id:
            self.select(idx)
        mon = self.mon
        shot = self.sct.grab(mon)
        cx, cy = cursor_pos()
        sig = (zlib.crc32(shot.bgra), cx, cy, scale, quality)
        if sig == self.last_sig and not force:
            return None
        self.last_sig = sig

        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if scale < 0.999:
            w = max(2, int(shot.width * scale) // 2 * 2)
            h = max(2, int(shot.height * scale) // 2 * 2)
            img = img.resize((w, h), Image.BILINEAR)
        else:
            scale = 1.0

        # mss does not capture the mouse pointer, so draw one.
        if IS_WINDOWS and mon["left"] <= cx < mon["left"] + mon["width"] \
                and mon["top"] <= cy < mon["top"] + mon["height"]:
            ox = (cx - mon["left"]) * scale
            oy = (cy - mon["top"]) * scale
            s = max(0.8, scale) * 1.1
            arrow = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
            pts = [(ox + px * s, oy + py * s) for px, py in arrow]
            d = ImageDraw.Draw(img)
            d.polygon(pts, fill=(255, 255, 255), outline=(0, 0, 0))

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=int(quality))
        return buf.getvalue()


# --------------------------------------------------------------------------
# Network server
# --------------------------------------------------------------------------
@dataclass(eq=False)
class Session:
    monitor: int = 1
    scale: float = 0.75
    quality: int = 60
    fps: int = 30
    inflight: int = 0
    force: bool = True
    inflight_since: float = 0.0
    refresh: bool = False


class ServerState:
    def __init__(self, pin: str, grabber_cls=Grabber, injector_cls=Injector,
                 max_clients: int = MAX_CLIENTS, allow_topology: bool = True,
                 topology_fn=None):
        self.pin = pin
        self.grabber_cls = grabber_cls
        self.injector_cls = injector_cls
        self.max_clients = max(1, min(MAX_CLIENTS, max_clients))
        self.allow_topology = allow_topology
        self.topology_fn = topology_fn or set_topology
        self.topology_lock = asyncio.Lock()
        self.sessions: set[Session] = set()
        self.fails: dict[str, list[float]] = {}   # ip -> [count, locked_until]

    def locked(self, ip: str) -> bool:
        rec = self.fails.get(ip)
        return bool(rec and rec[1] > time.time())

    def fail(self, ip: str):
        rec = self.fails.setdefault(ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= LOCKOUT_FAILS:
            rec[0] = 0
            rec[1] = time.time() + LOCKOUT_SECONDS

    def ok(self, ip: str):
        self.fails.pop(ip, None)


def apply_settings(sess: Session, m: dict) -> bool:
    """Returns True if the monitor changed."""
    changed_monitor = False
    if "scale" in m:
        sess.scale = _clamp(m["scale"], 0.2, 1.0, sess.scale)
    if "quality" in m:
        sess.quality = int(_clamp(m["quality"], 10, 95, sess.quality))
    if "fps" in m:
        sess.fps = int(_clamp(m["fps"], 5, 60, sess.fps))
    if "monitor" in m:
        new = int(_clamp(m["monitor"], 1, 32, sess.monitor))
        changed_monitor = new != sess.monitor
        sess.monitor = new
    sess.force = True
    return changed_monitor


async def send_loop(ws, sess: Session, grabber, ex, loop, on_refresh):
    last_sent = 0.0
    try:
        while True:
            now = time.perf_counter()
            if sess.refresh:
                sess.refresh = False
                await on_refresh()
                continue
            if sess.inflight >= MAX_INFLIGHT:
                if sess.inflight_since and now - sess.inflight_since > 3.0:
                    sess.inflight = 0          # phone stopped acking; recover
                await asyncio.sleep(0.003)
                continue
            force = sess.force or (now - last_sent > 2.0)
            sess.force = False
            try:
                data = await loop.run_in_executor(
                    ex, grabber.grab, sess.monitor, sess.scale, sess.quality, force)
            except Exception as e:            # UAC / lock screen / display change
                log.warning("capture failed: %s", e)
                await asyncio.sleep(0.5)
                continue
            if data is not None:
                sess.inflight += 1
                sess.inflight_since = time.perf_counter()
                last_sent = sess.inflight_since
                await ws.send(data)
            spent = time.perf_counter() - now
            await asyncio.sleep(max(0.0, 1.0 / max(1, sess.fps) - spent))
    except (ConnectionClosed, asyncio.CancelledError):
        pass
    except Exception:
        log.exception("send loop crashed")


async def handle_client(ws, st: ServerState):
    loop = asyncio.get_running_loop()
    ip = ws.remote_address[0] if ws.remote_address else "?"

    async def reply(obj):
        await ws.send(json.dumps(obj))

    if st.locked(ip):
        await reply({"type": "error", "message": "Too many wrong PINs. Wait a minute."})
        await ws.close(1008, "locked")
        return

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError
    except Exception:
        await ws.close(1008, "bad handshake")
        return

    given = str(msg.get("pin", "")).encode()
    if msg.get("type") != "auth" or not hmac.compare_digest(given, st.pin.encode()):
        st.fail(ip)
        log.warning("%s: wrong PIN", ip)
        await reply({"type": "error", "message": "Wrong PIN"})
        await ws.close(1008, "auth")
        return
    st.ok(ip)

    if len(st.sessions) >= st.max_clients:
        log.warning("%s rejected: server full", ip)
        await reply({"type": "error",
                     "message": f"PC is full ({st.max_clients} of {st.max_clients} devices connected)."})
        await ws.close(1008, "full")
        return

    sess = Session()
    st.sessions.add(sess)
    log.info("%s connected (%d/%d)", ip, len(st.sessions), st.max_clients)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    grabber = st.grabber_cls()
    injector = st.injector_cls(grabber)
    apply_settings(sess, msg)
    sender = None
    monitors: list[dict] = []
    try:
        monitors = await loop.run_in_executor(ex, grabber.monitor_list)
        if not monitors:
            await reply({"type": "error", "message": "No monitors found"})
            return
        if all(m["id"] != sess.monitor for m in monitors):
            sess.monitor = monitors[0]["id"]

        async def send_hello():
            info = await loop.run_in_executor(ex, grabber.select, sess.monitor)
            sess.monitor = info["id"]
            await reply({
                "type": "hello", "name": socket.gethostname(), "monitors": monitors,
                "monitor": info["id"], "width": info["width"], "height": info["height"],
                "mode": "extend" if len(monitors) > 1 else "mirror",
                "can_topology": bool(st.allow_topology),
                "clients": len(st.sessions), "max_clients": st.max_clients,
            })

        async def do_refresh():
            """Re-read the monitor layout after the PC's display mode changed."""
            nonlocal monitors
            await loop.run_in_executor(ex, grabber.refresh)
            monitors = await loop.run_in_executor(ex, grabber.monitor_list)
            if all(m["id"] != sess.monitor for m in monitors):
                sess.monitor = monitors[0]["id"]
            await send_hello()

        async def change_mode(mode: str):
            if not st.allow_topology:
                await reply({"type": "notice",
                             "message": "Mode switching is disabled on the PC (--no-topology)."})
                return
            async with st.topology_lock:
                ok = await loop.run_in_executor(None, st.topology_fn, mode)
                if not ok:
                    await reply({"type": "notice",
                                 "message": "Windows refused the display mode change."})
                    return
                await asyncio.sleep(1.5)            # let Windows settle
                for other in st.sessions:
                    if other is not sess:
                        other.refresh = True        # they re-read their layout
                await do_refresh()
                if mode == "extend":
                    extra = [m["id"] for m in monitors if m["id"] >= 2]
                    if not extra:
                        await reply({"type": "notice", "message":
                                     "No second display available to extend onto. Plug in a "
                                     "monitor or install a virtual display driver."})
                        return
                    used = {o.monitor for o in st.sessions if o is not sess}
                    free = [i for i in extra if i not in used]
                    sess.monitor = (free or extra)[0]
                else:
                    sess.monitor = 1
                sess.force = True
                await send_hello()

        await send_hello()
        sender = asyncio.create_task(send_loop(ws, sess, grabber, ex, loop, do_refresh))

        async for raw in ws:
            if not isinstance(raw, str):
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            t = m.get("type")
            if t == "ack":
                sess.inflight = max(0, sess.inflight - 1)
            elif t == "settings":
                if m.get("mode") in ("mirror", "extend"):
                    await change_mode(m["mode"])
                if apply_settings(sess, m):
                    await send_hello()
            elif t in ("mouse", "text", "key"):
                try:
                    injector.handle(m)
                except Exception:
                    log.exception("input failed")
    except ConnectionClosed:
        pass
    finally:
        if sender:
            sender.cancel()
            with contextlib.suppress(BaseException):
                await sender
        with contextlib.suppress(Exception):
            injector.release_all()
        with contextlib.suppress(Exception):
            await loop.run_in_executor(ex, grabber.close)
        ex.shutdown(wait=False, cancel_futures=True)
        st.sessions.discard(sess)
        log.info("%s disconnected (%d/%d)", ip, len(st.sessions), st.max_clients)


def discovery_thread(name: str, ws_port: int, disc_port: int):
    """Answers 'who is out there?' broadcasts from the phone."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", disc_port))
    except OSError as e:
        log.warning("auto-discovery disabled (%s) - enter the IP manually", e)
        return
    payload = json.dumps({"name": name, "port": ws_port}).encode()
    while True:
        try:
            data, addr = s.recvfrom(1024)
            if data.strip() == DISCOVERY_MAGIC:
                s.sendto(payload, addr)
        except OSError:
            time.sleep(0.5)


def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is actually sent
            ip = s.getsockname()[0]
            if ip not in ips and not ip.startswith("127."):
                ips.insert(0, ip)
    except OSError:
        pass
    return ips or ["<unknown>"]


async def run(pin: str, port: int = WS_PORT, grabber_cls=Grabber,
              injector_cls=Injector, ready: asyncio.Event | None = None,
              max_clients: int = MAX_CLIENTS, allow_topology: bool = True,
              topology_fn=None):
    st = ServerState(pin, grabber_cls, injector_cls, max_clients, allow_topology, topology_fn)

    async def handler(ws):
        await handle_client(ws, st)

    async with serve(handler, "0.0.0.0", port, max_size=2 ** 16,
                     ping_interval=15, ping_timeout=20, compression=None) as server:
        if ready is not None:
            ready.port = list(server.sockets)[0].getsockname()[1]   # type: ignore[attr-defined]
            ready.set()
        await asyncio.Future()


def main():
    ap = argparse.ArgumentParser(description="ScreenLink server")
    ap.add_argument("--pin", help="fixed PIN (default: random 6 digits each start)")
    ap.add_argument("--port", type=int, default=WS_PORT)
    ap.add_argument("--max-clients", type=int, default=MAX_CLIENTS,
                    help="phones connected at once, 1-3 (default 3)")
    ap.add_argument("--no-topology", action="store_true",
                    help="don't let phones switch the PC between Mirror and Extend")
    ap.add_argument("--list-monitors", action="store_true", help="print monitors and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.list_monitors:
        g = Grabber()
        for m in g.monitor_list():
            print(f"monitor {m['id']}: {m['width']}x{m['height']} at ({m['left']},{m['top']})")
        return

    if not IS_WINDOWS:
        log.warning("This server injects input with the Windows API; "
                    "streaming works here but mouse/keyboard control will not.")

    pin = args.pin or f"{secrets.randbelow(10 ** 6):06d}"
    threading.Thread(target=discovery_thread,
                     args=(socket.gethostname(), args.port, DISCOVERY_PORT),
                     daemon=True).start()

    print("\n==============  ScreenLink Server  ==============")
    print(f"  PIN:      {pin}")
    for ip in local_ips():
        print(f"  Address:  {ip}:{args.port}")
    print(f"  Up to {max(1, min(MAX_CLIENTS, args.max_clients))} phones at once.")
    print("  Phone and PC must be on the same Wi-Fi / hotspot.")
    print("  Press Ctrl+C to stop.")
    print("=================================================\n")

    try:
        asyncio.run(run(pin, args.port, max_clients=args.max_clients,
                        allow_topology=not args.no_topology))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()