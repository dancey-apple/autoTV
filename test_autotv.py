"""Tests for autotv, run against a fake Roku. Usage: python3 -m unittest"""

import contextlib
import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import autotv


class FakeRoku(ThreadingHTTPServer):
    def __init__(self, is_tv=True, power="DisplayOff", app=("", "Roku"), status=200):
        super().__init__(("127.0.0.1", 0), Handler)
        self.is_tv, self.power, self.app, self.status = is_tv, power, app, status
        self.screensaver = False
        self.posts = []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body=""):
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        s = self.server
        if self.path == "/query/device-info":
            self._send(
                "<device-info><user-device-name>Living Room</user-device-name>"
                f"<model-name>Test</model-name><is-tv>{str(s.is_tv).lower()}</is-tv>"
                f"<power-mode>{s.power}</power-mode><network-type>wifi</network-type>"
                "<wifi-mac>aa:bb:cc:dd:ee:ff</wifi-mac></device-info>"
            )
        elif self.path == "/query/active-app":
            app_id, name = s.app
            attr = f' id="{app_id}"' if app_id else ""
            saver = '<screensaver id="1">Saver</screensaver>' if s.screensaver else ""
            self._send(f"<active-app><app{attr}>{name}</app>{saver}</active-app>")
        else:
            self.send_error(404)

    def do_POST(self):
        s = self.server
        s.posts.append(self.path)
        if s.status == 200:
            if self.path == "/keypress/PowerOn":
                s.power = "PowerOn"
            elif self.path == "/keypress/PowerOff":
                s.power = "DisplayOff"
        self._send()


class AutoTVTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.videos = self.tmp / "videos.txt"
        self.videos.write_text("fish https://www.youtube.com/watch?v=aaaaaaaaaaa # reef\n"
                               "whale https://youtu.be/bbbbbbbbbbb\n")
        self.addCleanup(setattr, autotv.time, "sleep", autotv.time.sleep)
        autotv.time.sleep = lambda s: None

    def roku(self, **kw):
        server = FakeRoku(**kw)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def run_cli(self, server, *argv, config=""):
        port = server.server_address[1] if server else 1
        (self.tmp / "config.toml").write_text(f'host = "127.0.0.1"\nport = {port}\n{config}')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = autotv.main(["--config", str(self.tmp / "config.toml"),
                                "--videos", str(self.videos),
                                "--state", str(self.tmp / "state.json"), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_parse_video_id(self):
        for text in ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s",
                     "https://youtu.be/dQw4w9WgXcQ?si=abc", "youtube.com/live/dQw4w9WgXcQ",
                     "https://m.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ"):
            self.assertEqual(autotv.parse_video_id(text), "dQw4w9WgXcQ", text)
        for bad in ("", "https://example.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/playlist?list=PL1", "tooshort"):
            with self.assertRaises(autotv.AutoTVError, msg=bad):
                autotv.parse_video_id(bad)

    def test_pick_video(self):
        videos = [("fish", "a"), ("fish", "b"), ("whale", "c")]
        self.assertEqual(autotv.pick_video(videos, "whale"), ("whale", "c"))
        for _ in range(20):  # never repeats the last one when there is a choice
            self.assertEqual(autotv.pick_video(videos, "fish", last="a"), ("fish", "b"))
        self.assertEqual(autotv.pick_video(videos, "whale", last="c"), ("whale", "c"))
        with self.assertRaises(autotv.AutoTVError):
            autotv.pick_video(videos, "birds")

    def test_on_wakes_tv_and_plays(self):
        server = self.roku(power="DisplayOff")
        code, out = self.run_cli(server, "on", "whale")
        self.assertEqual(code, 0, out)
        self.assertEqual(server.posts, ["/keypress/PowerOn",
                                        "/launch/837?contentId=bbbbbbbbbbb&mediaType=movie"])
        state = autotv.load_state(self.tmp / "state.json")
        self.assertEqual(state["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(state["last_video"], "bbbbbbbbbbb")

    def test_on_player_just_launches(self):
        server = self.roku(is_tv=False, power="PowerOn")
        code, out = self.run_cli(server, "on", "fish")
        self.assertEqual(code, 0, out)
        self.assertEqual(server.posts, ["/launch/837?contentId=aaaaaaaaaaa&mediaType=movie"])

    def test_on_skips_when_busy(self):
        server = self.roku(power="PowerOn", app=("12", "Netflix"))
        code, out = self.run_cli(server, "on")
        self.assertEqual(code, 0, out)
        self.assertIn("Netflix", out)
        self.assertEqual(server.posts, [])

        code, out = self.run_cli(server, "on", "fish", "--force")
        self.assertEqual(len(server.posts), 1, out)

    def test_on_plays_over_screensaver(self):
        server = self.roku(power="PowerOn", app=("12", "Netflix"))
        server.screensaver = True
        self.run_cli(server, "on", "fish")
        self.assertEqual(len(server.posts), 1)

    def test_post_launch_keys(self):
        server = self.roku(power="PowerOn")
        self.run_cli(server, "on", "fish", config='post_launch_keys = ["Select"]\n')
        self.assertEqual(server.posts[-1], "/keypress/Select")

    def test_on_without_videos(self):
        self.videos.write_text("# nothing yet\n")
        server = self.roku()
        code, out = self.run_cli(server, "on")
        self.assertEqual(code, 1)
        self.assertIn("add fish", out)
        self.assertEqual(server.posts, [])

    def test_off(self):
        server = self.roku(power="PowerOn")
        self.run_cli(server, "off")
        self.assertEqual(server.posts, ["/keypress/PowerOff"])
        player = self.roku(is_tv=False, power="PowerOn")
        self.run_cli(player, "off")
        self.assertEqual(player.posts, ["/keypress/Home"])

    def test_refused_explains_setting(self):
        server = self.roku(power="PowerOn")
        server.status = 200
        code, _ = self.run_cli(server, "status")
        self.assertEqual(code, 0)
        server.status = 403
        code, out = self.run_cli(server, "status")
        self.assertEqual(code, 1)
        self.assertIn("Control by mobile apps", out)

    def test_video_option_needs_no_list(self):
        self.videos.write_text("")
        server = self.roku(power="PowerOn")
        code, out = self.run_cli(server, "on", "--video", "https://youtu.be/ddddddddddd")
        self.assertEqual(code, 0, out)
        self.assertIn("contentId=ddddddddddd", server.posts[0])

    def test_unreachable(self):
        code, out = self.run_cli(None, "status")
        self.assertEqual(code, 1)
        self.assertIn("not answering", out)

    def test_add_and_list(self):
        code, out = self.run_cli(None, "add", "Fish", "https://youtu.be/ccccccccccc", "big", "tank")
        self.assertEqual(code, 0, out)
        self.assertIn(("fish", "ccccccccccc"), autotv.load_videos(self.videos))
        self.assertIn("# big tank", self.videos.read_text())
        _, out = self.run_cli(None, "add", "fish", "ccccccccccc")
        self.assertIn("Already", out)
        _, out = self.run_cli(None, "list")
        self.assertEqual(len(out.strip().splitlines()), 3)

    def test_bad_config_key(self):
        code, out = self.run_cli(None, "list", config="hots = 1\n")
        self.assertEqual(code, 1)
        self.assertIn("hots", out)

    def test_wake_on_lan_rejects_bad_mac(self):
        with self.assertRaises(autotv.AutoTVError):
            autotv.wake_on_lan("nope")


if __name__ == "__main__":
    unittest.main()
