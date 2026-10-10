"""Talking to a hub, and remembering which hub we are talking to."""

import json
import os
import socket
import urllib.error
import urllib.request

# Where the hub URL, role and read cursor are remembered between commands.
# ~/.duet by default so an agent can run duet from any directory; set DUET_HOME
# to something else if two runs must share one machine.
SESSION_DIR = os.environ.get("DUET_HOME") or os.path.join(
    os.path.expanduser("~"), ".duet")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")


class HubError(Exception):
    """The hub refused, or could not be reached."""


# ---- session file ---------------------------------------------------------

def load_session():
    try:
        with open(SESSION_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_session(**kw):
    s = load_session()
    s.update(kw)
    os.makedirs(SESSION_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)
    return s


def hub_url(explicit=None):
    url = explicit or os.environ.get("DUET_HUB") or load_session().get("hub")
    if not url:
        raise HubError(
            "no hub known — run `duet open ...` (master) or `duet join <url>` (slave) first"
        )
    return url.rstrip("/")


def role_of(explicit=None):
    role = explicit or os.environ.get("DUET_ROLE") or load_session().get("role")
    if role not in ("master", "slave"):
        raise HubError("no role known — pass --as master|slave")
    return role


# ---- http -----------------------------------------------------------------

def request(url, path, payload=None, timeout=70):
    full = url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(full, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        raise HubError(body.get("error") or "hub returned HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise HubError("cannot reach the hub at %s (%s)" % (url, exc.reason))
    except socket.timeout:
        raise HubError("the hub did not answer in time")


def lan_ip():
    """Best guess at the address another machine can reach us on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
