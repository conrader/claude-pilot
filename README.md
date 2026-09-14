# claude-pilot

**Keep a Claude Code session working toward a goal while you are away.**

You start a session, give it a goal, walk off. `claude-pilot` resumes the session every time it goes quiet, feeds it any instructions you queued in the meantime, keeps it inside a tick and time budget, hands the work over to a fresh session before the context fills up, and tells you when the goal is done, when the session is blocked on something only you can supply, or when the budget ran out.

It is the autonomy loop I run my own projects on. The whole thing is one Python package with no dependencies, driven by a Claude Code `Stop` hook and a systemd user timer.

```
$ cd ~/code/my-app
$ claude            # work a bit, so a transcript exists
> /pilot Ship the CSV export: endpoint, tests, docs. Do not touch billing.

claude-pilot: my-app enrolled
  session   3f9c1a2e-…
  goal      Ship the CSV export: endpoint, tests, docs. Do not touch billing.
  bounds    40 ticks, until 2026-09-15T02:10:00+00:00

  The loop resumes this session when it goes quiet. To finish, say
  PILOT-DONE in the session, or run: claude-pilot stop my-app
```

Then close the laptop.

## How it works

```
 you ──/pilot "goal"──▶ registry (one JSON per pilot)
                              │
 Claude Code ──Stop hook──▶ hookd ──▶ wake marker ──▶ systemd .path ──▶ tick
                                                                      │
 systemd timer (every 10 min) ────────────────────────────────────────┤
                                                                      ▼
                           tick: for each active pilot
                             skip if a human has the session open in a terminal
                             skip if the transcript changed in the last 8 min
                             skip most idle ticks (backoff) unless there is new input
                             claude -r <session> -p "<goal + rules + your queued instructions>"
                             read the reply: PILOT-DONE / PILOT-BLOCKED / error / continue
                             hand off to a fresh session when the transcript gets large
```

Three ideas carry the design:

1. **The transcript is the ground truth.** Liveness is the mtime of the session's transcript file, not a process list or a hook event. A session that is writing its transcript is working and must not be resumed, because a second resume forks the conversation into two parallel chains on one working tree.
2. **A Stop hook proves a turn ended, not that a session ended.** The hook wakes the loop instantly instead of waiting for the timer, but a person with the session open in a terminal outranks every other signal. The loop leaves it alone.
3. **Bounds are real.** Forty ticks and twelve hours by default. A pilot that runs out is stopped and reported, not silently renewed. `--persistent` renews the bounds instead, for long-lived loops you switch off yourself.

## Install

Python 3.11+ on Linux (the interactive-session guard reads `/proc`; everything else is portable). Requires the `claude` CLI.

```
pip install git+https://github.com/conrader/claude-pilot
```

### 1. The Stop hook

Add to `~/.claude/settings.json` (merge with your existing hooks):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "[ -n \"$CLAUDE_PILOT_TICK\" ] || { cat | curl -s -m 3 -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8910/hook >/dev/null 2>&1 || true ; }" } ] }
    ]
  }
}
```

The guard on `CLAUDE_PILOT_TICK` keeps headless ticks from waking themselves. The `|| true` matters: a hook that fails interrupts the session that fired it, and the receiver is built the same way, it always answers 204.

### 2. The services

```
claude-pilot install-units --write     # writes four user units into ~/.config/systemd/user
systemctl --user enable --now claude-pilot-hookd.service claude-pilot.timer claude-pilot-wake.path
```

| unit | role |
|---|---|
| `claude-pilot-hookd.service` | receives hook events on `127.0.0.1:8910`, drops a wake marker when a piloted session stops |
| `claude-pilot-wake.path` + `.service` | runs a tick the moment a marker appears |
| `claude-pilot.timer` + `.service` | runs a tick every 10 minutes regardless |

No systemd? Run `claude-pilot hookd` under any supervisor and `claude-pilot tick` from cron. Ticks take a lock, so overlapping schedulers are safe.

### 3. The slash command

Copy `contrib/claude-code/commands/pilot.md` to `~/.claude/commands/pilot.md`. `/pilot <goal>` enrols the current session and tells the model the rules of the loop; `/pilot status` and `/pilot stop` do what they say.

## Commands

| command | what it does |
|---|---|
| `claude-pilot start "<goal>"` | enrol the newest session in the current directory. `--ticks`, `--hours`, `--persistent`, `--name`, `--session-id`, `--force` |
| `claude-pilot tick` | one pass over all pilots (what the timer and the wake path run) |
| `claude-pilot status [name]` | state, ticks, deadline, last replies |
| `claude-pilot tell <name> "<text>"` | queue an instruction; delivered on the next resume, put back if the resume fails |
| `claude-pilot inbox <name>` | show queued and delivered instructions |
| `claude-pilot goal <name> "<text>"` | change the goal of a running pilot |
| `claude-pilot stop <name>` | stop for good. A stop is final; `start --force` re-arms deliberately |
| `claude-pilot revive <name>` | bring back an expired, exhausted, errored or paused pilot with fresh bounds |
| `claude-pilot hookd` | the hook receiver in the foreground |
| `claude-pilot install-units [--write]` | render (or install) the systemd user units |

`start` refuses when the directory is not a project (no `.git`, no manifest), when no transcript exists for it yet, when a previous pilot there was stopped by a human, or when the concurrency cap (3) is reached. Every refusal says why.

## What the session is told

Each resume sends the goal, the tick and deadline, your queued instructions, and a short set of standing rules: work in steps that end verified and committed; read state back from the system of record instead of trusting an exit code; do not widen scope; a failed verification is a full stop; reply `PILOT-DONE` only when the goal is fully met and verified, `PILOT-BLOCKED` with what you need when only the human can unblock you; being still in progress is a fine state to be resumed in. The text lives in `claude_pilot/instruction.py` and is meant to be edited.

## Configuration

`~/.config/claude-pilot/config.json`, every key optional, every key overridable with `CLAUDE_PILOT_<KEY>` in the environment:

```json
{
  "agent_command": "claude",
  "model": "",
  "permission_mode": "bypassPermissions",
  "idle_minutes": 8,
  "idle_backoff": 6,
  "default_ticks": 40,
  "default_hours": 12,
  "cap": 3,
  "compact_tokens": 150000,
  "resume_timeout_s": 3600,
  "notify_command": "",
  "hookd_port": 8910
}
```

`permission_mode` is passed to `claude --permission-mode` on every headless turn. The default, `bypassPermissions`, is what makes an unattended loop possible at all: a `-p` turn has no terminal to answer a prompt, so anything short of it turns every file write into a false `PILOT-BLOCKED`. It also means the session can do whatever your goal and your `CLAUDE.md` allow, unprompted. Use `acceptEdits` if you want writes but no shell, and keep destructive rules in `CLAUDE.md`, where the model reads them every turn.

`notify_command` is any shell command; the message arrives on stdin. A Telegram bot, `mail`, `notify-send`, a webhook, whatever you read. Identical messages within ten minutes are sent once. State lives in `~/.local/state/claude-pilot/` (`CLAUDE_PILOT_HOME` to move it).

## Things it will not do

- Judge whether the session is doing the right thing. The loop enforces bounds and delivers instructions; the quality of the work is the model's and yours. A supervisor that reads transcripts and pauses off-goal pilots is a natural add-on and is where I put mine.
- Resume a session running on another machine, or a Codex session. Both existed in the original and were cut to keep this small; the seams are still visible in `driver.py` if you want them back.
- Protect you from a goal like "make it work" with no verification. Write goals with a definition of done.

## Lessons baked in

Every guard in `tick.py` was paid for. In order of cost:

- Two schedulers resumed the same session seconds apart and the pilot forked into two chains doing the work twice on a production checkout. Hence the tick lock, and hence "transcript mtime, not hook events" as the liveness signal.
- A stray wake marker with no matching pilot kept a `.path` unit restarting until systemd hit its start limit and silently gave up on every pilot. Hence markers are consumed first, in every state, before anything can `continue`.
- A resume that failed after draining the inbox ate the operator's instruction and nobody could tell why the pilot sat idle. Hence instructions go back when the turn did not happen.
- A pilot that finished its goal and was told to stay open spent a full model turn every ten minutes concluding "nothing changed". Hence idle backoff, which never delays a real instruction.
- Any path with a dot in it was unpilotable because the transcript directory name replaces every non-alphanumeric character, not only slashes. Hence `transcript_slug`.
- A Stop hook fired at the end of a human's turn and the loop resumed the session next to the person typing in it. Hence the interactive-session guard.

## Development

```
python3 -m pytest -q
```

Pure stdlib, tests included for every module. Files stay under 500 lines by rule. Issues and pull requests welcome; a bug report with the transcript directory listing and `claude-pilot status` output is usually enough.

## License

MIT.
