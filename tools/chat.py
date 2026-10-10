#!/usr/bin/env python3
"""A plain terminal chat with any OpenAI-compatible endpoint.

Not part of Duet — it is here because you always want to poke a model by hand
before you put it in a seat, and because "is the endpoint even up?" should not
need a Duet to answer.

    python3 tools/chat.py --api http://192.168.2.133:1234/v1 --model adversary

Inside: /models, /model <name>, /system <text>, /reset, /save <file>, /exit.
Answers stream as they are generated. Ctrl-C stops a reply; Ctrl-D exits.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_API = os.environ.get("DUET_API", "http://localhost:1234/v1")
THINK_OPEN = re.compile(r"<think>", re.I)
THINK_CLOSE = re.compile(r"</think>", re.I)


def post(api, path, payload, key, timeout=600, stream=False):
    req = urllib.request.Request(
        api.rstrip("/") + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    return urllib.request.urlopen(req, timeout=timeout)


def get(url, key, timeout=20):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def models(api, key):
    """Model ids with their load state, where the endpoint reports it."""
    root = api.rstrip("/").rsplit("/v1", 1)[0]
    try:                                   # LM Studio's richer endpoint
        return [(m["id"], m.get("state", "")) for m in
                get(root + "/api/v0/models", key)["data"]]
    except Exception:                      # noqa: BLE001 — any plain OpenAI server
        try:
            return [(m["id"], "") for m in get(api.rstrip("/") + "/models", key)["data"]]
        except Exception as exc:           # noqa: BLE001
            raise SystemExit("cannot list models at %s — %s" % (api, exc))


def stream_reply(api, key, model, history, temperature, max_tokens):
    """Print the answer as it arrives; return the whole text."""
    payload = {"model": model, "messages": history, "temperature": temperature,
               "max_tokens": max_tokens, "stream": True,
               "response_format": {"type": "text"}}
    parts, in_think = [], False
    try:
        resp = post(api, "/chat/completions", payload, key, stream=True)
    except urllib.error.HTTPError as exc:
        print("\n! HTTP %s — %s" % (exc.code, exc.read().decode("utf-8", "replace")[:500]))
        return None
    except Exception as exc:                                     # noqa: BLE001
        print("\n! %s: %s" % (type(exc).__name__, exc))
        return None

    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0].get("delta", {})
            except (ValueError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if not piece:
                continue
            # Dim a model's visible chain of thought rather than hiding it.
            if THINK_OPEN.search(piece):
                in_think = True
                sys.stdout.write("\033[2m")
            parts.append(piece)
            sys.stdout.write(piece)
            sys.stdout.flush()
            if in_think and THINK_CLOSE.search(piece):
                in_think = False
                sys.stdout.write("\033[0m")
    sys.stdout.write("\033[0m\n")
    return "".join(parts)


def main(argv=None):
    p = argparse.ArgumentParser(description="Chat with an OpenAI-compatible endpoint.")
    p.add_argument("--api", default=DEFAULT_API, help="base URL ending in /v1")
    p.add_argument("--model", help="model to talk to (default: the loaded one)")
    p.add_argument("--key", default=os.environ.get("DUET_API_KEY", "not-needed"))
    p.add_argument("--system", help="system prompt; - reads stdin")
    p.add_argument("--temperature", type=float, default=0.8)
    p.add_argument("--max-tokens", type=int, default=1200)
    args = p.parse_args(argv)

    available = models(args.api, args.key)
    model = args.model
    if not model:
        loaded = [m for m, state in available if state == "loaded"]
        model = loaded[0] if loaded else available[0][0]

    system = sys.stdin.read() if args.system == "-" else args.system
    history = [{"role": "system", "content": system}] if system else []

    print("%s · %s" % (args.api, model))
    print("  /models  /model <name>  /system <text>  /reset  /save <file>  /exit")
    while True:
        try:
            line = input("\n\033[1myou ›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue

        if line in ("/exit", "/quit"):
            return 0
        if line == "/models":
            for mid, state in available:
                print("  %-55s %s" % (mid, state))
            continue
        if line.startswith("/model "):
            model = line.split(None, 1)[1].strip()
            print("  → %s (history kept; /reset to clear it)" % model)
            continue
        if line.startswith("/system"):
            system = line[len("/system"):].strip()
            history = [{"role": "system", "content": system}] if system else []
            print("  system prompt set, history cleared")
            continue
        if line == "/reset":
            history = [{"role": "system", "content": system}] if system else []
            print("  history cleared")
            continue
        if line.startswith("/save "):
            path = line.split(None, 1)[1].strip()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(history, fh, indent=2, ensure_ascii=False)
            print("  saved %d messages to %s" % (len(history), path))
            continue
        if line.startswith("/"):
            print("  unknown command")
            continue

        history.append({"role": "user", "content": line})
        print("\n\033[1m%s ›\033[0m " % model, end="", flush=True)
        try:
            text = stream_reply(args.api, args.key, model, history,
                                args.temperature, args.max_tokens)
        except KeyboardInterrupt:
            sys.stdout.write("\033[0m\n  (stopped)\n")
            history.pop()
            continue
        if text is None:
            history.pop()
        else:
            history.append({"role": "assistant", "content": text})


if __name__ == "__main__":
    sys.exit(main())
