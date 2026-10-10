"""The Duet hub: holds the conversation, enforces the turn order, serves the page.

Pure standard library. One process, one run. State lives in memory and is mirrored
to .duet/transcript.json after every message so a finished run can be read back.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROLES = ("master", "slave")
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page", "index.html")


class Conflict(Exception):
    """The caller tried something the run's rules do not allow."""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Store:
    """The whole run. Every public method takes the lock itself."""

    def __init__(self, task="", sends=6, conversations=1, transcript=None):
        self.task = task
        self.sends = int(sends)
        self.conversations = int(conversations)
        self.transcript = transcript
        self.started = _now()
        self.status = "open"          # open | complete
        self.seq = 0
        self.version = 0
        self.convs = [self._new_conv(1)]
        self.cond = threading.Condition()

    # ---- internals (caller holds the lock) --------------------------------

    @staticmethod
    def _new_conv(index):
        return {"index": index, "opened": _now(), "closed": None, "messages": []}

    def _conv(self):
        return self.convs[-1]

    def _sends_used(self):
        return sum(1 for m in self._conv()["messages"] if m["role"] == "master")

    def _expected(self):
        """Whose turn it is in the current conversation."""
        if self.status == "complete":
            return None
        msgs = self._conv()["messages"]
        if not msgs:
            return "master"
        return "slave" if msgs[-1]["role"] == "master" else "master"

    def _advance(self):
        """Close the current conversation and open the next one, or finish."""
        self._conv()["closed"] = _now()
        if len(self.convs) >= self.conversations:
            self.status = "complete"
        else:
            self.convs.append(self._new_conv(len(self.convs) + 1))

    def _bump(self):
        self.version += 1
        self.cond.notify_all()
        self._save()

    def _save(self):
        if not self.transcript:
            return
        try:
            os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
            tmp = self.transcript + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._snapshot(), fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.transcript)
        except OSError:
            pass  # a transcript we cannot write must never break the run

    def _snapshot(self):
        return {
            "task": self.task,
            "sends": self.sends,
            "conversations": self.conversations,
            "started": self.started,
            "status": self.status,
            "version": self.version,
            "conversation": self._conv()["index"],
            "sends_used": self._sends_used(),
            "turn": self._expected(),
            "convs": self.convs,
        }

    # ---- public ------------------------------------------------------------

    def snapshot(self):
        with self.cond:
            return self._snapshot()

    def say(self, role, text):
        if role not in ROLES:
            raise Conflict("unknown role %r" % (role,))
        text = (text or "").strip()
        if not text:
            raise Conflict("empty message")
        with self.cond:
            if self.status == "complete":
                raise Conflict(
                    "the run is complete (%d conversations x %d sends); nothing more to say"
                    % (self.conversations, self.sends)
                )
            expected = self._expected()
            if role != expected:
                raise Conflict(
                    "it is the %s's turn, not the %s's — wait for the reply first"
                    % (expected, role)
                )
            if role == "master" and self._sends_used() >= self.sends:
                raise Conflict(
                    "conversation %d has used all %d sends"
                    % (self._conv()["index"], self.sends)
                )

            self.seq += 1
            conv = self._conv()
            msg = {
                "seq": self.seq,
                "role": role,
                "text": text,
                "ts": _now(),
                "conversation": conv["index"],
                "new_conversation": not conv["messages"] and conv["index"] > 1,
            }
            conv["messages"].append(msg)

            # A slave reply to the final send closes the conversation.
            if role == "slave" and self._sends_used() >= self.sends:
                self._advance()

            self._bump()
            return {
                "ok": True,
                "seq": msg["seq"],
                "conversation": msg["conversation"],
                "sends_used": self._sends_used(),
                "sends": self.sends,
                "conversations": self.conversations,
                "status": self.status,
                "turn": self._expected(),
            }

    def wait(self, role, after, timeout=25.0):
        """Block until a message from the other side arrives after `after`."""
        if role not in ROLES:
            raise Conflict("unknown role %r" % (role,))
        other = "slave" if role == "master" else "master"
        deadline = time.monotonic() + float(timeout)
        with self.cond:
            while True:
                for conv in self.convs:
                    for m in conv["messages"]:
                        if m["seq"] > after and m["role"] == other:
                            return {
                                "message": m,
                                "status": self.status,
                                "sends": self.sends,
                                "conversations": self.conversations,
                                "conversation": self._conv()["index"],
                                "sends_used": self._sends_used(),
                                "turn": self._expected(),
                                "task": self.task,
                            }
                if self.status == "complete":
                    return {"message": None, "status": "complete", "turn": None}
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"message": None, "status": self.status, "waiting": True}
                self.cond.wait(min(left, 1.0))

    def next_conversation(self):
        """End the current conversation early (master's choice)."""
        with self.cond:
            if self.status == "complete":
                raise Conflict("the run is already complete")
            if not self._conv()["messages"]:
                raise Conflict("this conversation has not started yet")
            self._advance()
            self._bump()
            return self._snapshot()

    def end(self):
        with self.cond:
            if self.status != "complete":
                self._conv()["closed"] = _now()
                self.status = "complete"
                self._bump()
            return self._snapshot()


class Handler(BaseHTTPRequestHandler):
    server_version = "duet/1"
    store = None          # set by serve()

    def log_message(self, fmt, *args):   # keep the console clean
        pass

    # ---- plumbing ----------------------------------------------------------

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ---- routes ------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"page missing", "text/plain")
            return
        if u.path == "/api/state":
            self._json(200, self.store.snapshot())
            return
        if u.path == "/api/wait":
            role = (q.get("role") or [""])[0]
            after = int((q.get("after") or ["0"])[0])
            timeout = min(float((q.get("timeout") or ["25"])[0]), 60.0)
            try:
                self._json(200, self.store.wait(role, after, timeout))
            except Conflict as exc:
                self._json(409, {"error": str(exc)})
            return
        self._json(404, {"error": "no such endpoint"})

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/say":
                self._json(200, self.store.say(body.get("role"), body.get("text")))
                return
            if u.path == "/api/next":
                self._json(200, self.store.next_conversation())
                return
            if u.path == "/api/end":
                self._json(200, self.store.end())
                return
        except Conflict as exc:
            self._json(409, {"error": str(exc)})
            return
        self._json(404, {"error": "no such endpoint"})


def serve(store, host="0.0.0.0", port=7788):
    Handler.store = store
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
