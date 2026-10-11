#!/usr/bin/env python3
"""The Duet console: set up a run in a browser and watch it happen.

One page. Type the master's brief, say how many sends and how many
conversations, pick what sits in the slave seat, press Start. The console opens
the hub, launches both sides as ordinary processes, and streams the chat back to
the page. Nothing here is required to use Duet -- it is the terminal commands
with a front end on them.

    python3 tools/console.py --port 8902

Runs whatever you ask it to run, with no authentication. LAN-only, like the hub.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
PAGE = os.path.join(TOOLS, "console.html")
RUNDIR = os.path.join(os.path.expanduser("~"), ".duet", "run")
DEFAULT_API = os.environ.get("DUET_API", "http://localhost:1234/v1")


def tail(path, n=40):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""


class Run:
    """One run: a hub, a master process, and maybe a slave process."""

    def __init__(self, hub_port):
        self.hub_port = hub_port
        self.procs = {}          # name -> Popen
        self.logs = {}           # name -> path
        self.config = None
        self.error = None
        self.lock = threading.Lock()

    # ---- process plumbing --------------------------------------------------

    @property
    def hub_url(self):
        return "http://127.0.0.1:%d" % self.hub_port

    def _spawn(self, name, cmd):
        os.makedirs(RUNDIR, exist_ok=True)
        path = os.path.join(RUNDIR, name + ".log")
        env = dict(os.environ,
                   PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   PYTHONUNBUFFERED="1")
        with open(path, "wb") as log:
            log.write(("$ " + " ".join(cmd) + "\n\n").encode("utf-8"))
        self.logs[name] = path
        log = open(path, "ab")
        self.procs[name] = subprocess.Popen(
            cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            cwd=REPO, env=env, start_new_session=True)

    def alive(self, name):
        p = self.procs.get(name)
        return bool(p and p.poll() is None)

    def stop(self):
        """Close the run and take every process down with it."""
        try:
            urllib.request.urlopen(
                urllib.request.Request(self.hub_url + "/api/end", data=b"{}",
                                       headers={"Content-Type": "application/json"}),
                timeout=3).close()
        except Exception:                                    # noqa: BLE001
            pass
        for name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    try:
                        p.terminate()
                    except Exception:                        # noqa: BLE001
                        pass
        time.sleep(0.4)
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:                            # noqa: BLE001
                    pass
        self.procs.clear()
        self._await_free_port()

    def _await_free_port(self, seconds=5.0):
        """The next run cannot bind the hub port until this one has let go."""
        import socket
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", self.hub_port))
                return True
            except OSError:
                time.sleep(0.2)
            finally:
                s.close()
        return False

    # ---- starting ----------------------------------------------------------

    def start(self, cfg):
        with self.lock:
            self.stop()
            self.error = None
            self.config = cfg

            task = (cfg.get("task") or "").strip()
            if not task:
                raise ValueError("the master needs a brief")
            sends = max(1, int(cfg.get("sends") or 6))
            convs = max(1, int(cfg.get("conversations") or 1))

            # The console owns the hub, so that stopping a run really ends it and
            # the next one cannot attach to the last one's leftovers.
            self._spawn("hub", [sys.executable, "-m", "duet", "open", "--foreground",
                                "--task", task,
                                "--sends", str(sends),
                                "--conversations", str(convs),
                                "--port", str(self.hub_port)])
            for _ in range(60):
                time.sleep(0.1)
                if self._hub_state():
                    break
                if not self.alive("hub"):
                    self.error = "the hub stopped at once — see its log"
                    return
            else:
                self.error = "the hub did not come up — see its log"
                return

            master = [sys.executable, os.path.join(TOOLS, "llm_agent.py"), "master",
                      "--api", cfg.get("api") or DEFAULT_API,
                      "--model", cfg.get("model") or "",
                      "--hub", self.hub_url,
                      "--sends", str(sends),
                      "--conversations", str(convs),
                      "--task", task]
            if cfg.get("temperature"):
                master += ["--temperature", str(float(cfg["temperature"]))]
            if not cfg.get("carry_lessons", True):
                master += ["--no-carry-lessons"]
            if (cfg.get("system") or "").strip():
                master += ["--system", cfg["system"]]
                if cfg.get("replace_system"):
                    master += ["--replace-system"]
            self._spawn("master", master)

            kind = cfg.get("slave") or "claude"
            persona = (cfg.get("slave_system") or "").strip()
            if kind == "claude":
                slave = [sys.executable, os.path.join(TOOLS, "claude_slave.py"),
                         "--hub", self.hub_url]
                if persona:
                    slave += ["--system", persona]
                if cfg.get("slave_model"):
                    slave += ["--model", cfg["slave_model"]]
                if cfg.get("slave_tools"):
                    slave += ["--allow-tools"]
                self._spawn("slave", slave)
            elif kind == "llm":
                slave = [sys.executable, os.path.join(TOOLS, "llm_agent.py"), "slave",
                         "--api", cfg.get("slave_api") or cfg.get("api") or DEFAULT_API,
                         "--model", cfg.get("slave_model") or cfg.get("model") or "",
                         "--hub", self.hub_url]
                if persona:
                    slave += ["--system", persona]
                self._spawn("slave", slave)
            # kind == "manual": nobody is seated; an agent joins by itself.

    # ---- reading -----------------------------------------------------------

    def _hub_state(self):
        try:
            with urllib.request.urlopen(self.hub_url + "/api/state", timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:                                    # noqa: BLE001
            return None

    def snapshot(self, lan):
        state = self._hub_state()
        return {
            "running": (self.alive("hub") and bool(state)
                        and state.get("status") != "complete"),
            "error": self.error,
            "state": state,
            "config": self.config,
            "hub": "http://%s:%d" % (lan, self.hub_port),
            "default_api": DEFAULT_API,
            "procs": {name: ("running" if self.alive(name) else "stopped")
                      for name in ("hub", "master", "slave") if name in self.logs},
            "logs": {name: tail(path, 30) for name, path in self.logs.items()},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "duet-console/1"
    run = None
    lan = "127.0.0.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._json(500, {"error": "console.html is missing"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/status":
            self._json(200, self.run.snapshot(self.lan))
            return
        if path == "/api/models":
            api = (urlparse(self.path).query.split("api=", 1)[-1] or DEFAULT_API)
            from urllib.parse import unquote
            api = unquote(api) or DEFAULT_API
            root = api.rstrip("/").rsplit("/v1", 1)[0]
            try:
                with urllib.request.urlopen(root + "/api/v0/models", timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._json(200, [{"id": m["id"], "state": m.get("state", "")}
                                 for m in data["data"]])
            except Exception:                                # noqa: BLE001
                try:
                    with urllib.request.urlopen(api.rstrip("/") + "/models",
                                                timeout=10) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    self._json(200, [{"id": m["id"], "state": ""} for m in data["data"]])
                except Exception as exc:                     # noqa: BLE001
                    self._json(502, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except ValueError:
            self._json(400, {"error": "bad json"})
            return
        if path == "/api/start":
            try:
                self.run.start(body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:                         # noqa: BLE001
                self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
                return
            self._json(200, self.run.snapshot(self.lan))
            return
        if path == "/api/stop":
            self.run.stop()
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Browser console for Duet runs.")
    p.add_argument("--port", type=int, default=8902, help="console port")
    p.add_argument("--hub-port", type=int, default=7788, help="port for the hub it opens")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--api", help="endpoint the page offers by default "
                                 "(overrides $DUET_API)")
    args = p.parse_args(argv)

    if args.api:
        globals()["DEFAULT_API"] = args.api

    Handler.run = Run(args.hub_port)
    Handler.lan = lan_ip()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("Duet console")
    print("  open  http://%s:%d" % (Handler.lan, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        Handler.run.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
