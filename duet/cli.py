"""The two commands each agent needs, and the ones a human needs.

Master:  duet open ...   then  duet say "..."      (prints the slave's reply)
Slave:   duet join URL   then  duet recv / duet reply "..."
"""

import argparse
import json
import os
import subprocess
import sys
import time

from . import client
from .client import HubError

RULE = "-" * 60


def out(*parts):
    print(*parts)
    sys.stdout.flush()


def note(msg):
    out("[duet] " + msg)


def _text_arg(value):
    """A message may be given inline, or as `-` to read it from stdin."""
    if value == "-":
        return sys.stdin.read()
    return value


def _wait_loop(url, role, after, patience):
    """Poll the hub's long-poll endpoint until something arrives."""
    deadline = time.monotonic() + patience
    while True:
        res = client.request(url, "/api/wait?role=%s&after=%d&timeout=25" % (role, after))
        if res.get("message") or res.get("status") == "complete":
            return res
        if time.monotonic() > deadline:
            raise HubError(
                "no answer from the other agent after %d seconds — is it running?" % patience
            )


def _show(label, text):
    out("")
    out("--- %s ---" % label)
    out(text.rstrip())
    out(RULE)


# ---- commands --------------------------------------------------------------

def cmd_open(args):
    """Start the hub on this machine and become the master."""
    task = _text_arg(args.task) if args.task else ""
    log_dir = os.path.abspath(client.SESSION_DIR)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "hub.log")

    cfg = {
        "task": task,
        "sends": args.sends,
        "conversations": args.conversations,
        "host": args.host,
        "port": args.port,
        "transcript": os.path.abspath(args.transcript) if args.transcript
                      else os.path.join(log_dir, "transcript.json"),
    }
    cfg_path = os.path.join(log_dir, "hub.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)

    local = "http://127.0.0.1:%d" % args.port
    if args.foreground:
        from .hub import Store, serve
        store = Store(task, args.sends, args.conversations, cfg["transcript"])
        httpd = serve(store, args.host, args.port)
        client.save_session(hub=local, role="master", cursor=0)
        note("hub in the foreground on %s — ctrl-c to stop" % local)
        httpd.serve_forever()
        return 0

    with open(log_path, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "duet", "_hub", cfg_path],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )

    for _ in range(50):
        time.sleep(0.1)
        try:
            client.request(local, "/api/state", timeout=2)
            break
        except HubError:
            continue
    else:
        raise HubError("the hub did not come up — see %s" % log_path)

    client.save_session(hub=local, role="master", cursor=0)
    lan = "http://%s:%d" % (client.lan_ip(), args.port)
    note("hub up. %d conversation(s) x %d sends each." % (args.conversations, args.sends))
    out("")
    out("  watch the chat    %s" % lan)
    out("  slave joins with  duet join %s" % lan)
    out("  transcript        %s" % cfg["transcript"])
    out("")
    note('you are the master. Open conversation 1 with: duet say "<your first message>"')
    return 0


def cmd_hub(args):
    """Internal: run the hub in this process (spawned by `duet open`)."""
    from .hub import Store, serve
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    store = Store(cfg["task"], cfg["sends"], cfg["conversations"], cfg["transcript"])
    serve(store, cfg["host"], cfg["port"]).serve_forever()
    return 0


def cmd_join(args):
    url = args.url.rstrip("/")
    state = client.request(url, "/api/state", timeout=10)
    client.save_session(hub=url, role=args.role, cursor=0)
    note("joined %s as the %s." % (url, args.role))
    note("the run is %d conversation(s) of %d sends."
         % (state["conversations"], state["sends"]))
    if args.role == "slave":
        note("now run `duet recv` to wait for the master's first message.")
    return 0


def cmd_say(args):
    """Master: send, then block until the slave answers."""
    url = client.hub_url(args.hub)
    text = _text_arg(args.text)
    res = client.request(url, "/api/say", {"role": "master", "text": text})
    note("conversation %d/%d · send %d/%d · sent, waiting for the slave…"
         % (res["conversation"], res["conversations"], res["sends_used"], res["sends"]))

    got = _wait_loop(url, "master", res["seq"], args.patience)
    msg = got.get("message")
    if not msg:
        note("RUN COMPLETE — the hub closed the run. Nothing more to send.")
        return 0

    _show("SLAVE REPLY", msg["text"])

    if got.get("status") == "complete":
        note("RUN COMPLETE — all %d conversation(s) done. Stop here; summarise what you found."
             % got.get("conversations", 0))
    elif got.get("conversation", msg["conversation"]) != msg["conversation"]:
        note("conversation %d finished. Your next `duet say` opens conversation %d/%d — "
             "the slave will be told it is a fresh conversation with no memory of this one."
             % (msg["conversation"], got["conversation"], got["conversations"]))
    else:
        left = got["sends"] - got["sends_used"]
        note("conversation %d/%d · %d send(s) left. Next: duet say \"<your reply>\""
             % (got["conversation"], got["conversations"], left))
    return 0


def _deliver(got, args):
    """Slave: print whatever the master just said, or report the run is over."""
    msg = got.get("message")
    if not msg:
        note("RUN COMPLETE — the master has finished. Stop here, do not call duet again.")
        return 0
    if msg.get("new_conversation"):
        out("")
        out("=== NEW CONVERSATION (%d/%d) — this is a fresh start. Forget everything "
            "said in the previous conversation. ==="
            % (msg["conversation"], got.get("conversations", 0)))
    _show("MASTER SAYS", msg["text"])
    client.save_session(cursor=msg["seq"])
    note('reply with: duet reply "<your answer>"')
    return 0


def cmd_recv(args):
    url = client.hub_url(args.hub)
    cursor = client.load_session().get("cursor", 0)
    note("waiting for the master…")
    return _deliver(_wait_loop(url, "slave", cursor, args.patience), args)


def cmd_reply(args):
    """Slave: answer, then block until the master speaks again."""
    url = client.hub_url(args.hub)
    text = _text_arg(args.text)
    client.request(url, "/api/say", {"role": "slave", "text": text})
    note("sent. Waiting for the master…")
    return _deliver(_wait_loop(url, "slave", client.load_session().get("cursor", 0),
                               args.patience), args)


def cmd_next(args):
    url = client.hub_url(args.hub)
    state = client.request(url, "/api/next", {})
    if state["status"] == "complete":
        note("RUN COMPLETE — that was the last conversation.")
    else:
        note("conversation %d/%d starts with your next `duet say`."
             % (state["conversation"], state["conversations"]))
    return 0


def cmd_end(args):
    client.request(client.hub_url(args.hub), "/api/end", {})
    note("run closed.")
    return 0


def cmd_status(args):
    s = client.request(client.hub_url(args.hub), "/api/state")
    out("task          %s" % (s["task"] or "(none given)"))
    out("status        %s" % s["status"])
    out("conversation  %d/%d" % (s["conversation"], s["conversations"]))
    out("sends used    %d/%d (this conversation)" % (s["sends_used"], s["sends"]))
    out("turn          %s" % (s["turn"] or "-"))
    out("messages      %d" % sum(len(c["messages"]) for c in s["convs"]))
    return 0


def cmd_log(args):
    s = client.request(client.hub_url(args.hub), "/api/state")
    for conv in s["convs"]:
        out("")
        out("=== conversation %d/%d ===" % (conv["index"], s["conversations"]))
        for m in conv["messages"]:
            out("")
            out("[%s] %s:" % (m["ts"], m["role"].upper()))
            out(m["text"])
    return 0


# ---- argument parsing ------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="duet",
        description="Two agents, one chat. A master drives, a slave answers, "
                    "you watch on a webpage.",
    )
    sub = p.add_subparsers(dest="cmd")

    def common(sp, patience=900):
        sp.add_argument("--hub", help="hub URL (default: the one you opened/joined)")
        sp.add_argument("--patience", type=int, default=patience,
                        help="seconds to wait for the other agent (default %d)" % patience)

    o = sub.add_parser("open", help="master: start the hub and the run")
    o.add_argument("--task", help="what the master is testing (shown on the page); - reads stdin")
    o.add_argument("--sends", type=int, default=6, help="master messages per conversation")
    o.add_argument("--conversations", type=int, default=1, help="how many conversations to run")
    o.add_argument("--port", type=int, default=7788)
    o.add_argument("--host", default="0.0.0.0")
    o.add_argument("--transcript", help="where to mirror the chat as JSON "
                                        "(default ~/.duet/transcript.json)")
    o.add_argument("--foreground", action="store_true", help="do not detach the hub")
    o.set_defaults(func=cmd_open)

    h = sub.add_parser("_hub", help=argparse.SUPPRESS)
    h.add_argument("config")
    h.set_defaults(func=cmd_hub)

    j = sub.add_parser("join", help="slave: point yourself at the master's hub")
    j.add_argument("url")
    j.add_argument("--as", dest="role", default="slave", choices=["master", "slave"])
    j.set_defaults(func=cmd_join)

    s = sub.add_parser("say", help="master: send a message and print the slave's reply")
    s.add_argument("text", help="your message, or - to read it from stdin")
    common(s)
    s.set_defaults(func=cmd_say)

    r = sub.add_parser("recv", help="slave: wait for the master's next message")
    common(r)
    r.set_defaults(func=cmd_recv)

    y = sub.add_parser("reply", help="slave: answer, then wait for the master's next message")
    y.add_argument("text", help="your answer, or - to read it from stdin")
    common(y)
    y.set_defaults(func=cmd_reply)

    n = sub.add_parser("next", help="master: end this conversation early, start the next")
    common(n, 30)
    n.set_defaults(func=cmd_next)

    e = sub.add_parser("end", help="close the run now")
    common(e, 30)
    e.set_defaults(func=cmd_end)

    st = sub.add_parser("status", help="where the run stands")
    common(st, 30)
    st.set_defaults(func=cmd_status)

    lg = sub.add_parser("log", help="print the whole chat as text")
    common(lg, 30)
    lg.set_defaults(func=cmd_log)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except HubError as exc:
        out("[duet] %s" % exc)
        return 1
    except KeyboardInterrupt:
        return 130
