# Duet

Created: 2026-10-10

## Goal
A much simpler Parley: a master agent and a slave agent chat with each other over a tiny
HTTP hub, so Sven can test one agent with another. Chat only — no file sharing, no
shared state, no deck/stats. The master is given a task, a number of sends per
conversation and a number of conversations; Sven follows the chat live on a webpage
served by the hub on the master's machine.

Success: point two arbitrary agents (Claude, Codex, a local LLM agent) at the repo, tell
one "you are master, here is your task, 6 sends, 3 conversations" and the other "you are
slave, here is the URL", and the whole run happens by itself while Sven reads along.

## Status
**Working end to end.** Built and verified on 2026-10-10:
- `duet/hub.py` — Store (conversations, turn order, send/conversation limits,
  auto-advance, transcript mirror) + a ThreadingHTTPServer serving the API and the page.
- `duet/client.py` — HTTP helpers + the session file (`~/.duet/session.json`, override
  with `DUET_HOME`).
- `duet/cli.py` — `open / join / say / recv / reply / next / end / status / log`.
- `duet/page/index.html` — live chat page, polls `/api/state` every second.
- `bin/duet` — wrapper so agents can call it from any directory.
- `AGENTS.md` — the briefing an agent reads when handed the repo + a role.
- `tests/test_duet.py` — 9 tests over real HTTP, all passing.
- Full 2 conversations × 2 sends run driven through the real CLI: both sides' output,
  the auto-advance, the NEW CONVERSATION banner and the post-run refusal all correct.
- Page JS executed under node against real state: renders, no errors. (Headless Firefox
  screenshots do not work on this machine — don't waste time on them again.)

Not done: not a git repo yet, not pushed to GitHub. Sven has not asked for that.

## Key decisions
- **Hub + join, not a driver loop.** Sven's call: both sides are real agents that get the
  repo link and a role, so the harness must not call models itself. It only relays.
- **One command per exchange, blocking.** `duet say` posts and blocks until the reply is
  in; `duet reply` posts and blocks until the next master message. Agents never poll,
  sleep, or track cursors — that is what made this usable for LLM agents at all. Server
  long-poll is 25 s, the client loops until `--patience` (default 900 s).
- **The hub enforces the counts, not the agents.** Master-first, strict alternation, max
  `--sends` master messages per conversation, max `--conversations`. Violations are a 409
  whose message says whose turn it is. Agents cannot quietly run long.
- **Conversations auto-advance** when the slave answers the master's last send; the
  slave's next message carries `new_conversation`, rendered as a loud banner telling it
  to forget the previous conversation. Each conversation is an independent trial.
- **Session state in `~/.duet`, not cwd** — an agent that changes directory would
  otherwise lose the hub. `DUET_HOME` separates two sides on one machine.
- **No SSE, just 1 s polling** on the page — fewer moving parts, and the chat is slow.
- Stdlib only, Python 3.9+, like Parley.

## Next steps
1. Try a real run: Claude Code as master, Codex (or an LM Studio agent) as slave.
2. If it is to be published: `git init`, MIT LICENSE file, push, and fix the
   `github.com/Sfeeen/duet` clone URL in README.md/AGENTS.md if the repo gets another
   name.
3. Possible later, only if a real run asks for it: a `--brief` flag so the slave's
   persona is handed out by the hub, and per-conversation notes from the master.

## Notes
- Default port 7788. The hub binds 0.0.0.0 and has **no authentication** — anyone on the
  LAN who has the URL can post as either role. Fine for the bench, not for anything else.
- Transcript lands in `~/.duet/transcript.json` unless `--transcript` says otherwise.
- `duet open` detaches the hub with `start_new_session=True`; its stdout/stderr go to
  `~/.duet/hub.log`. Stop it with `duet end` plus `pkill -f "duet _hub"`.
- Parley lives at ~/Desktop/Projects/parley; Duet deliberately shares none of its code.
