#!/usr/bin/env python3
"""A one-page web chat for any OpenAI-compatible endpoint.

Run it anywhere on the LAN and open the URL it prints. It serves one HTML page
and proxies the streaming chat API, so the browser never has to reach the model
host directly and no key ever leaves this process.

    python3 tools/webchat.py --api http://192.168.2.133:1234/v1 --port 8899

Like the rest of this repo: standard library only, nothing to install.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_API = os.environ.get("DUET_API", "http://localhost:1234/v1")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webchat.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "duet-webchat/1"
    api = DEFAULT_API
    key = "not-needed"

    def log_message(self, fmt, *args):
        pass

    def _head(self, code, ctype, length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self._head(code, "application/json", len(raw))
        self.wfile.write(raw)

    def _upstream(self, path, payload=None, timeout=60):
        """Call the model endpoint. `path` is relative to its /v1 base."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.api.rstrip("/") + path, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.key})
        return urllib.request.urlopen(req, timeout=timeout)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._head(500, "text/plain")
                self.wfile.write(b"webchat.html is missing")
                return
            self._head(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return

        if path == "/models":
            root = self.api.rstrip("/").rsplit("/v1", 1)[0]
            try:                       # LM Studio reports load state here
                req = urllib.request.Request(
                    root + "/api/v0/models",
                    headers={"Authorization": "Bearer " + self.key})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._json(200, [{"id": m["id"], "state": m.get("state", ""),
                                  "type": m.get("type", "")} for m in data["data"]])
                return
            except Exception:                                    # noqa: BLE001
                pass
            try:
                with self._upstream("/models", timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._json(200, [{"id": m["id"], "state": "", "type": ""}
                                 for m in data["data"]])
            except Exception as exc:                             # noqa: BLE001
                self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/chat":
            self._json(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            self._json(400, {"error": "bad json"})
            return

        payload = {
            "model": body.get("model"),
            "messages": body.get("messages") or [],
            "temperature": body.get("temperature", 0.8),
            "max_tokens": body.get("max_tokens", 1200),
            "stream": True,
            "response_format": {"type": "text"},
        }
        try:
            up = self._upstream("/chat/completions", payload, timeout=900)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            self._json(502, {"error": "HTTP %s from the model host: %s"
                                      % (exc.code, detail)})
            return
        except Exception as exc:                                 # noqa: BLE001
            self._json(502, {"error": "cannot reach %s — %s" % (self.api, exc)})
            return

        # Pass the upstream SSE straight through to the browser.
        self._head(200, "text/event-stream")
        try:
            with up:
                for line in up:
                    self.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass            # the tab was closed mid-answer


def main(argv=None):
    p = argparse.ArgumentParser(description="Web chat for an OpenAI-compatible endpoint.")
    p.add_argument("--api", default=DEFAULT_API, help="base URL ending in /v1")
    p.add_argument("--key", default=os.environ.get("DUET_API_KEY", "not-needed"))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8899)
    args = p.parse_args(argv)

    Handler.api, Handler.key = args.api, args.key
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()

    print("web chat for %s" % args.api)
    print("  open  http://%s:%d" % (ip, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
