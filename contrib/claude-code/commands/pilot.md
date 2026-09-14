---
description: Put this session in a loop with claude-pilot and work autonomously toward a goal until it is done
argument-hint: <the goal, in one sentence> | stop | status
---

You are being enrolled in **claude-pilot**: a loop that keeps this session working toward a
goal when the human is away from the keyboard.

The argument is: `$ARGUMENTS`

## If the argument is `status`

Run `claude-pilot status`, show the output, and stop. Nothing else.

## If the argument is `stop`

Run `claude-pilot status` to find this directory's pilot name, then `claude-pilot stop <name>`,
report it, and stop.

## Otherwise: enrol, then work

**1. Enrol.** From your current working directory, run:

```
claude-pilot start "$ARGUMENTS"
```

Read what it prints: the pilot name, the session id, the tick and deadline bounds. If it refuses
(not a project directory, no recorded session, a previous pilot here was stopped, cap reached), say
so plainly and stop. Do not work around it; the refusal means the loop could not bound you.

**2. Restate the goal before starting.** In two or three sentences: what "done" means concretely,
how you will know it is done, and what you will NOT touch. If the goal is ambiguous enough that two
readings would produce different work, say which reading you are taking. Then begin.

**3. Work in steps that end somewhere.** Each stretch should finish with something verified and
committed, not a half-applied change. The loop may resume you at any point after you go quiet;
whatever you leave behind is what your next tick starts from.

**4. Verify at the last hop, always.** Read state back from the system of record rather than
trusting a success message. An exit code or a tool saying "OK" is not evidence.

**5. Finish honestly.** When the goal is fully met *and verified*, reply with

```
PILOT-DONE
```

on its own line, then a short summary of what was delivered and what you verified. If you are
blocked on something only the human can supply (a credential, a decision, an access), reply with

```
PILOT-BLOCKED
```

on its own line, then exactly what you need and why you cannot proceed without it.

**Do not claim `PILOT-DONE` for partial work.** Another tick costs a few minutes; a false finish
costs the human's trust in the whole loop. Being still in progress is a perfectly good state to be
resumed in.

## What is watching you

- `claude-pilot tick` runs every 10 minutes and the moment your session stops. It resumes you only
  if your transcript has been quiet for 8 minutes, so it will not interrupt you mid-task.
- Bounds are real: 40 ticks and 12 hours by default. When they run out the pilot stops and tells the
  human it did not finish.

## The standing rules still apply

Everything in this project's `CLAUDE.md` and in your global instructions binds you exactly as in a
normal session. Autonomy is permission to proceed through the agreed plan without asking at each
step. It is not permission to widen scope, to take a destructive or irreversible action that was not
in the plan, or to skip verification. When a verification fails, stop: a red result is a full stop.
