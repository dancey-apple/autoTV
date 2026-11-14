#!/usr/bin/env python3
"""autotv - wake a Roku from a Raspberry Pi and play ambiance videos on YouTube.

Talks to the Roku over its local-network External Control Protocol (ECP,
HTTP on port 8060). Standard library only; needs Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
YOUTUBE_APP_ID = "837"

DEFAULTS = {
    "host": "",  # empty = find the Roku with SSDP
    "port": 8060,
    "mac": "",  # empty = learn it from the Roku the first time it answers
    "category": "any",
    "skip_if_busy": True,
    "wake_timeout": 45,
    "post_launch_delay": 10,
    "post_launch_keys": [],
}


class AutoTVError(Exception):
    pass


# --------------------------------------------------------------------------
# Config, videos, state
# --------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise AutoTVError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
        cfg.update(data)
    return cfg


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2) + "\n")
    except OSError as e:
        print(f"warning: could not save {path}: {e}", file=sys.stderr)


_ID = r"[A-Za-z0-9_-]{11}"


def parse_video_id(text: str) -> str:
    """Accept a bare 11-character video ID or any common YouTube URL form."""
    text = text.strip()
    if re.fullmatch(_ID, text):
        return text
    url = urllib.parse.urlparse(text if "//" in text else "https://" + text)
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = ""
    if host == "youtu.be":
        candidate = url.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        query = urllib.parse.parse_qs(url.query)
        if "v" in query:
            candidate = query["v"][0]
        else:
            parts = url.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("live", "embed", "shorts", "v"):
                candidate = parts[1]
    if not re.fullmatch(_ID, candidate):
        raise AutoTVError(f"not a YouTube video link or ID: {text!r}")
    return candidate


def load_videos(path: Path) -> list[tuple[str, str]]:
    """Read `category  link-or-id  # note` lines into (category, video_id)."""
    videos = []
    if not path.exists():
        return videos
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise AutoTVError(f"{path}:{n}: expected `category link`, got {line!r}")
        try:
            videos.append((parts[0].lower(), parse_video_id(parts[1])))
        except AutoTVError as e:
            raise AutoTVError(f"{path}:{n}: {e}") from None
    return videos


def pick_video(videos, category: str, last: str | None = None) -> tuple[str, str]:
    category = category.lower()
    pool = [v for v in videos if category == "any" or v[0] == category]
    if not pool:
        have = sorted({c for c, _ in videos})
        hint = f" (categories you have: {', '.join(have)})" if have else ""
        raise AutoTVError(
            f"no videos for category {category!r}{hint}. "
            "Add one with: autotv.py add fish <youtube link>"
        )
    fresh = [v for v in pool if v[1] != last]
    return random.choice(fresh or pool)


# --------------------------------------------------------------------------
# Network: SSDP discovery, Wake-on-LAN, ECP
# --------------------------------------------------------------------------


def discover(timeout: float = 3.0) -> list[str]:
    """Return the hosts of Rokus answering an SSDP search on the local network."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "Host: 239.255.255.250:1900\r\n"
        'Man: "ssdp:discover"\r\n'
        "ST: roku:ecp\r\n"
        "MX: 2\r\n\r\n"
    ).encode()
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.5)
        deadline = time.monotonic() + timeout
        try:
            for _ in range(2):  # UDP is lossy, ask twice
                sock.sendto(msg, ("239.255.255.250", 1900))
        except OSError as e:
            raise AutoTVError(f"could not send discovery request: {e}") from None
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(rb"(?im)^location:\s*http://([^:/\s]+)", data)
            if m and m.group(1).decode() not in found:
                found.append(m.group(1).decode())
    return found


def wake_on_lan(mac: str) -> None:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(digits) != 12:
        raise AutoTVError(f"bad MAC address: {mac!r}")
    packet = b"\xff" * 6 + bytes.fromhex(digits) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            try:
                sock.sendto(packet, ("255.255.255.255", port))
            except OSError:
                pass


class Roku:
    def __init__(self, host: str, port: int = 8060, timeout: float = 4.0):
        self.host, self.port, self.timeout = host, port, timeout

    def _request(self, method: str, path: str) -> bytes:
        url = f"http://{self.host}:{self.port}{path}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AutoTVError(
                    f"the Roku refused {path} (HTTP {e.code}). On the Roku, set Settings > "
                    "System > Advanced system settings > Control by mobile apps > "
                    "Network access to Default (or Permissive)."
                ) from None
            raise AutoTVError(f"the Roku answered HTTP {e.code} for {path}") from None

    def device_info(self) -> dict:
        root = ET.fromstring(self._request("GET", "/query/device-info"))
        return {child.tag: (child.text or "") for child in root}

    def reachable(self) -> bool:
        try:
            self.device_info()
            return True
        except (OSError, ET.ParseError):
            return False  # a refusal (AutoTVError) is an answer, so let it through

    def active_app(self) -> dict:
        """Return {'id', 'name', 'screensaver'} for what is on screen now."""
        root = ET.fromstring(self._request("GET", "/query/active-app"))
        app = root.find("app")
        return {
            "id": app.get("id", "") if app is not None else "",
            "name": (app.text or "") if app is not None else "",
            "screensaver": root.find("screensaver") is not None,
        }

    def keypress(self, key: str) -> None:
        self._request("POST", "/keypress/" + urllib.parse.quote(key))

    def launch(self, app_id: str, **params: str) -> None:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        self._request("POST", f"/launch/{app_id}{query}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


class App:
    def __init__(self, args):
        self.config_path = Path(args.config)
        self.videos_path = Path(args.videos)
        self.state_path = Path(args.state)
        self.cfg = load_config(self.config_path)
        self.state = load_state(self.state_path)
        self.verbose = not args.quiet

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def mac(self) -> str:
        return self.cfg["mac"] or self.state.get("mac", "")

    def connect(self, wake: bool = False) -> Roku:
        """Find the Roku, waking it over the network first if asked to."""
        host = self.cfg["host"] or self.state.get("host", "")
        if not host:
            self.log("Looking for a Roku on the network...")
            hosts = discover()
            if not hosts and wake and self.mac():
                wake_on_lan(self.mac())
                hosts = discover(timeout=8)
            if not hosts:
                raise AutoTVError(
                    "no Roku found. Check that the Pi and Roku are on the same network, "
                    "or set `host` in config.toml to the Roku's IP address."
                )
            if len(hosts) > 1:
                self.log(f"Found several Rokus ({', '.join(hosts)}); using {hosts[0]}. "
                         "Set `host` in config.toml to choose another.")
            host = hosts[0]

        roku = Roku(host, self.cfg["port"])
        if not roku.reachable():
            if not wake:
                raise AutoTVError(f"the Roku at {host} is not answering")
            self.log(f"Roku at {host} is not answering; trying to wake it...")
            deadline = time.monotonic() + self.cfg["wake_timeout"]
            while not roku.reachable():
                if time.monotonic() > deadline:
                    raise AutoTVError(
                        f"the Roku at {host} did not wake up. On a Roku TV, turn on "
                        "Settings > System > Power > Fast TV start. If its IP address "
                        "changed, give it a fixed one in your router."
                    )
                if self.mac():
                    wake_on_lan(self.mac())
                time.sleep(2)

        info = roku.device_info()
        on_wifi = info.get("network-type") == "wifi"
        learned = info.get("wifi-mac" if on_wifi else "ethernet-mac") or info.get("wifi-mac", "")
        self.state["host"] = host
        if learned:
            self.state["mac"] = learned
        save_state(self.state_path, self.state)
        return roku

    def cmd_discover(self, args) -> int:
        hosts = discover()
        if not hosts:
            print("No Roku found.")
            return 1
        for host in hosts:
            try:
                info = Roku(host, self.cfg["port"]).device_info()
                name = info.get("user-device-name") or info.get("friendly-device-name", "")
                print(f"{host}  {name}  ({info.get('model-name', '?')})")
            except (OSError, ET.ParseError, AutoTVError):
                print(host)
        return 0

    def cmd_status(self, args) -> int:
        roku = self.connect()
        info = roku.device_info()
        app = roku.active_app()
        name = info.get("user-device-name") or info.get("friendly-device-name", "")
        print(f"Roku:     {name} at {roku.host} ({info.get('model-name', '?')})")
        print(f"Kind:     {'Roku TV' if info.get('is-tv') == 'true' else 'Roku player'}")
        print(f"Power:    {info.get('power-mode', 'unknown')}")
        print(f"Showing:  {app['name'] or 'nothing'}{' (screensaver)' if app['screensaver'] else ''}")
        if info.get("ecp-setting-mode"):
            print(f"Control:  {info['ecp-setting-mode']}")
        return 0

    def cmd_on(self, args) -> int:
        if args.video:
            category, video_id = "chosen", parse_video_id(args.video)
        else:
            category, video_id = pick_video(
                load_videos(self.videos_path),
                args.category or self.cfg["category"],
                self.state.get("last_video"),
            )

        roku = self.connect(wake=True)
        info = roku.device_info()
        is_tv = info.get("is-tv") == "true"
        awake = info.get("power-mode") == "PowerOn"

        if awake and self.cfg["skip_if_busy"] and not args.force:
            app = roku.active_app()
            idle = app["screensaver"] or app["id"] == "" or app["name"] in ("Roku", "Home")
            if not idle:
                self.log(f"The TV is showing {app['name']}; leaving it alone "
                         "(use --force to play anyway).")
                return 0

        if is_tv and not awake:
            self.log("Turning the TV on...")
            roku.keypress("PowerOn")
            deadline = time.monotonic() + self.cfg["wake_timeout"]
            while roku.device_info().get("power-mode") != "PowerOn":
                if time.monotonic() > deadline:
                    raise AutoTVError("the TV did not report that it turned on")
                time.sleep(1)
            time.sleep(3)  # let the home screen settle before launching

        self.log(f"Playing {category} video {video_id} on YouTube...")
        roku.launch(YOUTUBE_APP_ID, contentId=video_id, mediaType="movie")
        self.state["last_video"] = video_id
        save_state(self.state_path, self.state)

        keys = self.cfg["post_launch_keys"]
        if keys:
            time.sleep(self.cfg["post_launch_delay"])
            for key in keys:
                roku.keypress(key)
                time.sleep(1)
        return 0

    def cmd_off(self, args) -> int:
        roku = self.connect()
        info = roku.device_info()
        if info.get("is-tv") == "true":
            if info.get("power-mode") == "PowerOn":
                self.log("Turning the TV off...")
                roku.keypress("PowerOff")
            else:
                self.log("The TV is already off.")
        else:
            # A Roku player cannot power the TV down; stop playback and go home.
            self.log("Stopping playback (a Roku player cannot switch the TV off)...")
            roku.keypress("Home")
        return 0

    def cmd_add(self, args) -> int:
        video_id = parse_video_id(args.link)
        category = args.category.lower()
        if not re.fullmatch(r"[a-z0-9_-]+", category):
            raise AutoTVError("category must be one word, like fish or whale")
        if (category, video_id) in load_videos(self.videos_path):
            print("Already in the list.")
            return 0
        old = self.videos_path.read_text() if self.videos_path.exists() else ""
        if old and not old.endswith("\n"):
            old += "\n"
        note = f"  # {' '.join(args.note)}" if args.note else ""
        self.videos_path.write_text(f"{old}{category}  https://youtu.be/{video_id}{note}\n")
        print(f"Added {video_id} to {category}.")
        return 0

    def cmd_list(self, args) -> int:
        videos = load_videos(self.videos_path)
        if not videos:
            print("No videos yet. Add one with: autotv.py add fish <youtube link>")
            return 1
        for category, video_id in videos:
            print(f"{category:10} https://youtu.be/{video_id}")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=HERE / "config.toml", help="settings file")
    p.add_argument("--videos", default=HERE / "videos.txt", help="video list file")
    p.add_argument("--state", default=HERE / "state.json", help="remembered Roku address")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    on = sub.add_parser("on", help="turn the TV on and play an ambiance video")
    on.add_argument("category", nargs="?", help="fish, whale, ... (default: from config)")
    on.add_argument("--video", help="play this YouTube link or ID instead of a random one")
    on.add_argument("--force", action="store_true", help="play even if the TV is in use")
    sub.add_parser("off", help="turn the TV off")
    sub.add_parser("status", help="show what the Roku is doing")
    sub.add_parser("discover", help="list Rokus on the network")
    add = sub.add_parser("add", help="add a video to the list")
    add.add_argument("category")
    add.add_argument("link")
    add.add_argument("note", nargs="*", help="optional description")
    sub.add_parser("list", help="show the video list")

    args = p.parse_args(argv)
    try:
        app = App(args)
        return getattr(app, "cmd_" + args.command)(args)
    except AutoTVError as e:
        print(f"autotv: {e}", file=sys.stderr)
        return 1
    except (OSError, ET.ParseError) as e:
        print(f"autotv: lost contact with the Roku: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
