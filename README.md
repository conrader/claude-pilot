<p align="center">
  <img src="docs/assets/hero.svg" alt="claude-pilot: Give it a goal. Let it keep going. On your computer, VPS, or across your servers." width="100%">
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-82B7AA?style=flat-square&amp;labelColor=202528" alt="Python 3.11 or newer"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/platform-Linux-82B7AA?style=flat-square&amp;labelColor=202528" alt="Platform: Linux"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/runtime_dependencies-0-E6B48F?style=flat-square&amp;labelColor=202528" alt="Zero runtime dependencies"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-E6B48F?style=flat-square&amp;labelColor=202528" alt="MIT license"></a>
</p>

<h1 align="center">claude-pilot</h1>

<p align="center"><strong>Keep a Claude Code session working toward a goal while you are away.</strong></p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-is-this-different-from-goal">vs /goal</a> ·
  <a href="#computers-vps-and-multiple-servers">Multiple hosts</a> ·
  <a href="#codex-sessions">Codex</a> ·
  <a href="#plugging-in-your-own-second-brain">Your second brain</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#troubleshooting">Troubleshooting</a>
</p>

Give an existing session a clear goal. `claude-pilot` resumes it when it goes quiet, delivers instructions you queue along the way, and attempts a handoff to a fresh session when the transcript grows large. It records when the session reports completion, needs your help, or reaches its configured bounds.

It is built for more than one session at a time. One hook receiver and one tick loop drive a registry of pilots, each with its own goal, inbox, bounds and history, under a concurrency cap. Everything the loop knows is a JSON file, so an external orchestrator, your own scripts or a second brain can read every pilot's state and steer each one separately.

One Python package. Standard library only. A Claude Code `Stop` hook for prompt wakeups, with a systemd timer as a fallback.

Run it on your Linux computer, a VPS, or several servers: one controller drives Claude Code and Codex sessions on any number of hosts over SSH.

## Why use it?

| Keep work moving | Keep control |
| --- | --- |
| **Resume quiet sessions.** Continue toward the same goal across headless turns. | **Bound the loop.** Default limits of 40 ticks and 12 hours. |
| **Steer as you go.** Queue instructions without replacing the goal. | **Yield to your terminal.** An open interactive Claude session takes priority. |
| **Carry context forward.** Request a handoff before starting a fresh session. | **See the outcome.** Track status and plug in your own notifications. |

<p align="center">
  <img src="docs/assets/terminal.svg" alt="Illustrative terminal workflow: start a pilot for a CSV export, queue an instruction to include UTF-8 coverage, then inspect the active pilot's status." width="100%">
</p>

*Illustrative workflow; commands are copyable below. The project name, session ID, dates, and progress vary by run.*

## Quick start

### 1. Install

You need **Linux**, **Python 3.11+**, and an installed, authenticated `claude` CLI. The setup below also uses Git, curl, and systemd user services. The interactive-session guard depends on Linux `/proc`.

```bash
git clone https://github.com/conrader/claude-pilot.git
cd claude-pilot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

# Add /pilot to Claude Code.
mkdir -p ~/.claude/commands
cp contrib/claude-code/commands/pilot.md ~/.claude/commands/pilot.md
```

Keep this checkout and its virtual environment in place: the generated services use the installed executable's path. Activate the same environment when using `claude-pilot` in another shell.

Already have your own Python environment? Install directly with `python -m pip install git+https://github.com/conrader/claude-pilot.git`; copy the [slash command](contrib/claude-code/commands/pilot.md) separately if you want `/pilot`.

### 2. Connect the Stop hook

Merge this into `~/.claude/settings.json`, preserving your existing settings and hooks. The same snippet is available in [settings-hooks.json](contrib/claude-code/settings-hooks.json).

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "[ -n \"$CLAUDE_PILOT_TICK\" ] || { cat | curl -s -m 3 -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8910/hook >/dev/null 2>&1 || true ; }"
          }
        ]
      }
    ]
  }
}
```

`CLAUDE_PILOT_TICK` prevents headless turns from waking themselves. The short timeout and `|| true` let your session continue if the receiver is unavailable.

### 3. Start the services

```bash
claude-pilot install-units --write
systemctl --user enable --now \
  claude-pilot-hookd.service \
  claude-pilot.timer \
  claude-pilot-wake.path

# Confirm the hook receiver is responding.
curl -fsS http://127.0.0.1:8910/health
# ok
```

The installer writes **five unit files** to `~/.config/systemd/user/` and requests a daemon reload.

| Unit | Purpose |
| --- | --- |
| `claude-pilot-hookd.service` | Receive Stop events; create wake markers for matching active pilots. |
| `claude-pilot-wake.path` | Watch for wake markers. |
| `claude-pilot-wake.service` | Run a tick when the path watcher fires. |
| `claude-pilot.timer` | Schedule periodic ticks, with a 10-minute interval. |
| `claude-pilot.service` | Run a tick when the timer fires. |

**No systemd?** Run `claude-pilot hookd` under your supervisor and schedule `claude-pilot tick` with cron. The periodic tick consumes wake markers too; immediate wakeups need a separate watcher. Tick execution is locked against overlapping schedulers.

> [!IMPORTANT]
> The computer or VPS running the pilot must remain awake. Your own laptop can disconnect when the pilot runs on another host. For work after logout, the host's systemd user manager must remain running too; where permitted, `loginctl enable-linger "$USER"` enables that. The services also need access to your Claude authentication and executable. If `claude` is outside their PATH, set `agent_command` to its absolute path in [configuration](#configuration).

### 4. Give it a goal

Open Claude Code in the project you want to work on. Let it make at least one tool call so a transcript exists, then run:

```text
/pilot Ship the CSV export: endpoint, tests, docs. Do not touch billing.
```

When you want the background loop to take over, exit the interactive Claude session after the current work finishes. **Leaving it open in a terminal makes the pilot wait.**

You can also enroll the newest recorded session from your shell:

```bash
cd ~/code/my-app
claude-pilot start \
  "Ship the CSV export: endpoint, tests, docs. Do not touch billing." \
  --name my-app --ticks 40 --hours 12

claude-pilot tell my-app "Include UTF-8 coverage in the export tests."
claude-pilot status my-app
```

> [!NOTE]
> Headless turns default to `permission_mode: "bypassPermissions"`, which bypasses Claude Code permission prompts. Set the mode deliberately before running a pilot. Goals and `CLAUDE.md` guide the model; they are not an execution sandbox. Other permission modes can leave unattended work waiting for approval.

## How is this different from `/goal`?

Claude Code ships `/goal <condition>`: after every turn a small model judges whether the condition holds and, if not, the session takes another turn. It is the right tool when you are still at the keyboard, and it needs no setup. `claude-pilot` solves a different problem: the work has to continue after the session, the terminal and possibly the machine you typed on are gone.

| | `/goal` (built in) | `claude-pilot` |
| --- | --- | --- |
| Where the loop runs | Inside the live session process | Outside it: a scheduler resumes the session headlessly |
| Sessions at once | One, the one you are in | Many: a registry of pilots with a cap, one inbox each, one status view, one hook receiver |
| An external orchestrator can drive it | No | Yes: state files, `tell`, `status`, `stop` per pilot, from scripts or other hosts |
| Survives closing the terminal | Only while the session process is alive | Yes; the session is resumed by id from its transcript |
| Runs on a server with nobody logged in | No | Yes, from systemd or cron, on one host or many |
| Judges whether the goal is met | Yes, an evaluator model after every turn | Not by itself; the session reports `PILOT-DONE`, and `judge_command` lets your own supervisor pause it |
| Agents | Claude Code | Claude Code and Codex, in one registry |
| Bounds | A clause in the condition, e.g. "or stop after 20 turns" | Ticks and hours, enforced outside the model, plus a concurrency cap |
| Steering mid-run | Type in the session | `claude-pilot tell` from any shell or script, delivered on the next resume |
| Context growth | Auto-compaction inside the session | Handoff to a fresh session with a written summary once the transcript is large |
| Waking on events | Turn-driven | Stop hook wakes it instantly; a timer covers everything else |
| Notifications | On screen | Any command: chat bot, mail, webhook |
| Setup | None | A hook, three units, a config file |

The difference shows when there are several sessions: three repositories with three goals, one of them blocked on you, one idle, one chewing through a migration. `/goal` is a property of one conversation. A pilot registry is a fleet view of all of them, and every entry can be steered without opening it.

They compose. Start a pilot around a session that also carries a `/goal`: the built-in evaluator drives the turns while you are present, and the pilot takes over when the session goes quiet or ends. The pilot's own check is deliberately dumb: it trusts the reply markers and leaves judgement to you or to whatever supervisor you plug in through `notify_command` and the state files.

## Computers, VPS, and multiple servers

One `claude-pilot` installation can drive sessions on several machines. The host running the loop (the *controller*) keeps the registry, the scheduler and the hook receiver; each *execution host* runs its own Claude Code or Codex sessions on its own checkout. The controller reaches them over SSH: it lists transcripts, measures liveness and resumes turns remotely, and the execution hosts report their Stop events back to the controller's hook receiver.

<p align="center">
  <img src="docs/assets/deployment.svg" alt="Multiple-host deployment: a controller drives independent Claude and Codex sessions on a Linux computer, a VPS and a build server over SSH, while each host keeps its own checkout and authentication." width="100%">
</p>

Declare the hosts once, in the controller's config:

```json
{
  "hosts": {
    "dev-vps":      { "ssh": "me@dev-vps",      "key": "~/.ssh/pilot_ed25519" },
    "build-server": { "ssh": "me@build-server", "key": "~/.ssh/pilot_ed25519", "claude": "/opt/claude/bin/claude" }
  }
}
```

Then enrol a session where it lives and steer it from where you sit:

```bash
claude-pilot hosts                                   # reachability and the agent binaries found on each host
claude-pilot start "Migrate the schema, keep the tests green." --host dev-vps --name migration
claude-pilot start "Fix the flaky build." --host build-server --agent codex --name build
claude-pilot tell migration "Skip the audit table for now."
claude-pilot status
```

What each side needs:

| On the controller | On every execution host |
| --- | --- |
| The config above and a private key the hosts accept | `claude` or `codex` installed and authenticated **non-interactively**: file-based credentials work; a macOS keychain is locked inside an SSH session and will answer "Not logged in" |
| The hook receiver reachable from the hosts, with a token file (see below) | The Stop hook from [settings-hooks-remote.json](contrib/claude-code/settings-hooks-remote.json), which adds `X-Pilot-Host` and the token to the same curl |
| `python3` on the hosts, used for transcript scans | The project checkout, and at least one recorded session in it |

Off-loopback hook delivery is what the token file is for: put the same secret in `~/.config/claude-pilot/hookd.token` on the controller and on each host, and the receiver binds all interfaces and checks `X-Pilot-Token`. Without the file it listens on loopback only. The Stop hook of a remote session wakes the controller exactly like a local one; a remote Codex session asks the controller for its continuation decision over SSH (see [Codex](#codex-sessions)).

What stays out of scope: moving a running session between machines (handoffs start the fresh session on the same host) and a shared registry across several controllers. One controller, many hosts.

## Codex sessions

Pilots can drive [Codex](https://github.com/openai/codex) sessions as well as Claude Code ones, side by side in the same registry: `--agent codex`. Codex has no `-r` to resume a conversation headlessly, so the loop uses Codex's own Stop hook. Install [contrib/codex](contrib/codex/) on the host that runs Codex: its `hooks.json` registers the hook, and the hook script reports every session event to the receiver and, at each turn boundary, asks `claude-pilot codex-stop` whether to continue. The answer is either `{}` (let the turn end: goal reported done, blocked, budget or deadline reached, paused by your judge) or a `block` decision carrying the next instruction, which is how a Codex hook tells the model to keep going. The tick loop still watches Codex pilots: if the hook chain goes idle, it continues the session with `codex exec resume`, gated by the rollout file's age so it never races a live turn.

Claude and Codex pilots share everything else: bounds, inbox, `tell`, `status`, notifications, the judge and the context connector. What Codex pilots do not get is a transcript-size handoff; Codex manages its own context.

## Plugging in your own second brain

The registry is a directory of JSON files and every command is scriptable, so an external system can already watch and steer pilots. Two optional settings let it reach *into* the loop:

| Setting | When it runs | Contract |
| --- | --- | --- |
| `context_command` | before every resume | The pilot record as JSON on stdin. Whatever it prints is appended to the instruction under "Context from your operator's system". Project notes, guardrails, the last review, a memory lookup: one command, your choice. |
| `judge_command` | after every turn | `{"record": …, "reply": …}` on stdin. Print `{"verdict": "ok"}` to continue or `{"verdict": "pause", "reason": "…"}` to park the pilot in `paused` with that reason until you `revive` it. |

Both are time-boxed to 30 seconds and can never break a tick: a failing or malformed command counts as "no context" and "ok", and is logged. This is the seam where a supervisor that reads transcripts, checks the diff against the goal, or consults a knowledge base belongs; the loop itself stays deliberately dumb.

## How it works

### Two triggers, one loop

```mermaid
flowchart TD
    stop["Claude Code Stop hook"] --> receiver["Local hook receiver"]
    receiver --> wake["Wake marker + path watcher"]
    operator["Your queued instruction"] --> wake
    wake --> tick["Tick loop · exclusive lock"]
    timer["Periodic timer"] --> tick
    state[("Pilot records + inbox")] <--> tick
    tick --> guards{"Eligible to resume?"}
    guards -->|Yes| resume["Headless Claude Code turn"]
    guards -->|No| wait["Wait for a later tick"]
    resume --> state
```

Each tick examines enrolled pilots in order. Before resuming one, it checks its state and bounds, yields to an interactive Claude process, and applies liveness and idle-backoff rules. The resume prompt includes the goal, current bounds, standing instructions, and queued messages.

| Signal | What the loop does |
| --- | --- |
| Interactive Claude session in the project | Wait, even if a wake marker exists. |
| Fresh transcript activity without a wake marker | Wait; the default quiet window is 8 minutes, including subagent transcripts. |
| Stop hook or `tell` wake marker | Bypass the quiet-window and idle-backoff checks, while still honoring the interactive guard and bounds. |
| Quiet session with no new input | Resume on the first eligible idle tick, then back off; default `idle_backoff` is 6. |
| Failed resume after draining instructions | Put those instructions back in the inbox. |

### A fresh session, with a handoff

After a successful continuing turn, the loop estimates transcript tokens. At the default threshold of **150,000**, it attempts this handoff:

```mermaid
sequenceDiagram
    participant P as Pilot loop
    participant A as Current session
    participant B as Fresh session
    participant T as Local transcripts
    P->>A: Request state, next steps, and gotchas
    A-->>P: Handoff text
    alt Request succeeds and handoff is at least 200 characters
        P->>B: Start with the goal and handoff
        B-->>P: CLI result
        P->>T: Look for a new session ID
        T-->>P: New ID, if recorded
        Note over P,T: Switch only after success and a different ID
    else Handoff fails or is too short
        Note over P,A: Keep the current session
    end
```

This is a text-based handoff, using an approximate token count. If the fresh turn fails or no different session ID is found, the pilot keeps its original session reference. See [driver.py](claude_pilot/driver.py) and [sessions.py](claude_pilot/sessions.py).

### Clear outcomes

| Outcome | Pilot state | Next step |
| --- | --- | --- |
| Reply contains `PILOT-DONE` | `done` | Review the delivered work. |
| Reply contains `PILOT-BLOCKED` | `blocked` | Supply what is missing, then `revive`. |
| Deadline reached | `expired` | `revive` extends an elapsed deadline. |
| Tick budget reached | `exhausted` | `revive` starts a fresh tick budget. |
| Resume fails | `error` | Inspect the last reply, fix the cause, then `revive`. |
| You run `stop` | `stopped` | Re-arming requires explicit `--force`. |

Completion and blocking are **reported by the model**. The loop recognizes reply markers; it does not independently verify the implementation. Normal successful replies keep the pilot active. A detected background-agent conflict also keeps it active and does not spend a tick.

Notifications go to stdout and, when configured, your `notify_command`. Identical messages are deduplicated for ten minutes.

## Commands

| Command | What it does |
| --- | --- |
| `claude-pilot start "<goal>"` | Enroll the newest transcript in the current project. Supports `--name`, `--cwd`, `--session-id`, `--ticks`, `--hours`, `--persistent`, `--force`, `--allow-more`, `--agent {claude,codex}` and `--host NAME`. |
| `claude-pilot tick` | Run one pass over enrolled pilots. |
| `claude-pilot status [name]` | Show state, tick count, deadline, goal, and the latest recorded reply. |
| `claude-pilot tell <name> "<text>"` | Queue an instruction and create a wake marker. |
| `claude-pilot inbox <name>` | Show pending instructions. `--drain` marks them consumed. |
| `claude-pilot goal <name> "<text>"` | Replace the goal without resetting ticks or deadline; also reactivate a done, blocked, errored, expired, or exhausted pilot. |
| `claude-pilot stop <name>` | Mark a pilot stopped to prevent future resumes. |
| `claude-pilot revive <name>` | Reactivate an existing pilot; extend an elapsed deadline; reset the tick budget if it was exhausted. Supports `--hours`, `--persistent`, `--force`, and `--allow-more`. |
| `claude-pilot hookd` | Run the hook receiver in the foreground; supports `--port` and `--bind`. |
| `claude-pilot hosts` | Check every configured execution host: reachable, and which agent binaries it has. |
| `claude-pilot codex-stop` | The Codex Stop-hook controller; reads the hook payload on stdin, prints the decision. Wired by [contrib/codex](contrib/codex/). |
| `claude-pilot install-units [--write]` | Print the systemd units, or install them. |

Inside Claude Code, use `/pilot <goal>`, `/pilot status`, or `/pilot stop`.

By default, `start` requires a project directory with `.git` or a recognized manifest, plus an existing transcript. The active-pilot cap is **3**. Refusals explain why enrollment could not proceed.

<details>
<summary><strong>Bounds, recovery, and persistent pilots</strong></summary>

- Bounds are checked before resuming; the deadline does not interrupt a turn already running. `resume_timeout_s` limits each CLI invocation.
- The tick budget counts resume attempts, not tokens or money. Handoff calls can add model turns beyond that count.
- `revive` preserves the tick counter, except for an exhausted pilot, which gets a fresh budget. Changing a goal preserves the existing bounds.
- `--persistent` renews elapsed deadlines and resets exhausted tick counters. It can still finish, block, or error; it does not override `stop`.
- Recognized usage-limit errors are retried after at least 60 minutes. That recovery can extend an elapsed deadline, but does not clear an exhausted tick counter.
- `stop` updates the record; it is not a process-kill command. Let an in-flight tick finish, then confirm the state with `status`.

</details>

## Configuration

Create `~/.config/claude-pilot/config.json`. Every key is optional; environment variables named `CLAUDE_PILOT_<KEY>` override file values, which override built-in defaults.

<details>
<summary><strong>Show all defaults</strong></summary>

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
  "context_command": "",
  "judge_command": "",
  "codex_command": "codex",
  "codex_sandbox": "",
  "ssh_command": "ssh",
  "hosts": {},
  "hookd_port": 8910,
  "hookd_token_file": "~/.config/claude-pilot/hookd.token"
}
```

</details>

| Setting | Use it to… |
| --- | --- |
| `agent_command` | Point to your Claude executable, using an absolute path when needed by systemd. |
| `model` / `permission_mode` | Forward model selection and permission mode to each headless turn. |
| `idle_minutes` / `idle_backoff` | Control quiet-session detection and how often idle pilots resume. |
| `default_ticks` / `default_hours` / `cap` | Set default enrollment bounds and the active-pilot cap. |
| `compact_tokens` / `resume_timeout_s` | Set the handoff threshold and per-invocation timeout. |
| `notify_command` | Run your notification command with the message on stdin. |
| `context_command` / `judge_command` | Feed context into every resume and judge every turn; see [second brain](#plugging-in-your-own-second-brain). |
| `codex_command` / `codex_sandbox` | The Codex binary and an optional `sandbox_mode` for `codex exec`. |
| `hosts` / `ssh_command` | Execution hosts reachable over SSH; see [multiple servers](#computers-vps-and-multiple-servers). |
| `hookd_port` / `hookd_token_file` | Configure the receiver port and optional off-loopback authentication. |

For example, to use desktop notifications where `notify-send` and a desktop session are available:

```json
{
  "notify_command": "xargs -0 notify-send 'claude-pilot'"
}
```

Without a token file, the receiver defaults to `127.0.0.1`. If a nonempty token file is present, its default bind address becomes `0.0.0.0`, and off-loopback requests require a matching `X-Pilot-Token` header. Change the hook URL too if you change the port.

| Path override | Default |
| --- | --- |
| `CLAUDE_PILOT_HOME` | `~/.local/state/claude-pilot/`: records, inboxes, events, wake markers, and lock. |
| `CLAUDE_PILOT_CONFIG` | `~/.config/claude-pilot/config.json` |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects/`: Claude Code transcripts. |

Shell environment overrides apply to commands started from that shell. For systemd services, use the JSON config or explicitly configure their environment.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| “No transcript recorded” | Open Claude in the target directory and let it make a tool call before enrolling. |
| Pilot stays active but does not resume | Exit an open interactive Claude session; inspect the quiet window and idle backoff. |
| `claude` works in your shell but fails under systemd | Set `agent_command` to the full path returned by `command -v claude`. Confirm authentication is available to the service user. |
| Stop hooks do not wake the pilot | Check `/health`, the hook settings, and `claude-pilot-wake.path`. |
| Instructions wait on a blocked pilot | `tell` queues input but does not reactivate the pilot; run `revive` after supplying the missing information. |

```bash
claude-pilot status
systemctl --user status \
  claude-pilot-hookd.service claude-pilot.timer claude-pilot-wake.path
journalctl --user \
  -u claude-pilot.service -u claude-pilot-wake.service \
  -u claude-pilot-hookd.service -n 80 --no-pager
```

## Design notes

**Transcript activity is the liveness signal.** Without a wake marker, recent writes to the session or its subagent transcripts mean the loop leaves it alone. A Stop hook accelerates scheduling; a human's interactive terminal still takes priority.

**Scheduling must survive duplicate triggers.** A file lock serializes ticks. Wake markers are consumed before state checks, and orphaned markers are removed so a path watcher cannot restart forever.

**Operator input must survive a failed turn.** Drained inbox messages are put back if the resume fails. Idle backoff never delays a wake marker or pending instruction, though the other guards still apply.

**The goal defines the work.** Each resume asks the model to stay in scope, finish verified and committed steps, read state back, and report `PILOT-DONE` only for completed work. Those prompts live in [instruction.py](claude_pilot/instruction.py).

Each instance manages Claude Code sessions on its execution host; [multiple hosts](#computers-vps-and-multiple-servers) can be controlled through an external layer. The package does not judge the work itself; `judge_command` is where your supervisor plugs in.

## Development

From your activated virtual environment in the checkout:

```bash
python -m pip install -e . pytest
python -m pytest -q
```

The application uses only the Python standard library; pytest is a development dependency. Tests cover the CLI, configuration, session discovery, driver, hooks, registry, instructions, notifications, and tick loop. Source files stay under 500 lines by project convention.

Issues and pull requests are welcome. Include the observed behavior and relevant `claude-pilot status` output; remove private goals, paths, and credentials before sharing logs.

## License

[MIT](LICENSE) · Built by [Konrad Sierzputowski](https://github.com/conrader).
