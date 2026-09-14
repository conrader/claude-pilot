"""The resume prompt sent to a pilot each tick, and the handoff/compaction prompts."""
from __future__ import annotations

DONE = "PILOT-DONE"
BLOCKED = "PILOT-BLOCKED"


def build_instruction(rec: dict, queued: list[dict]) -> str:
    """Compose the prompt for one resume tick.

    Includes the standing rules, any instructions queued in the pilot's
    inbox since the last tick, and the goal and tick bounds.
    """
    inbox_block = ""
    if queued:
        lines = "\n".join(f"- {q.get('text', '')}" for q in queued)
        inbox_block = (
            "Instructions from your operator since the last tick:\n"
            f"{lines}\n\n"
        )

    return (
        f"You are being resumed by claude-pilot (tick {rec['ticks'] + 1} of "
        f"{rec['max_ticks']}, deadline {rec.get('deadline', 'none')}). "
        f"Goal: {rec['goal']}\n\n"
        "Work in steps that end verified and committed. Verify at the last "
        "hop by reading state back; never trust an exit code or a success "
        "message alone. Do not widen scope beyond the goal. A failed "
        "verification is a full stop, not a reason to guess and continue.\n\n"
        f"When the goal is fully met AND verified, reply with {DONE} on its "
        "own line followed by a short summary. If you are blocked on "
        f"something only a human can supply, reply with {BLOCKED} followed "
        f"by exactly what you need. Never claim {DONE} for partial work. If "
        "there is nothing new to do this tick, say so briefly and stop; a "
        "short reply is fine. Nobody is watching this turn: do not ask questions, "
        "decide within the goal and proceed, or reply "
        f"{BLOCKED} if the decision is genuinely the human's.\n\n"
        f"{inbox_block}"
    ).rstrip() + "\n"


def handoff_request() -> str:
    """Ask the current session to write a concise handoff before compaction."""
    return (
        "Before this session ends, write a concise handoff for the session "
        "that will continue this work: current state, next steps, and any "
        "gotchas a fresh session would otherwise rediscover the hard way. "
        "Keep it short and specific."
    )


def handoff_opening(handoff_text: str, rec: dict) -> str:
    """The first prompt given to the fresh session after compaction."""
    return (
        "You are continuing a pilot's work in this directory. The previous "
        "session's transcript grew too large and was retired; this is a "
        "fresh session picking up from its handoff.\n\n"
        f"GOAL: {rec['goal']}\n\n"
        "Handoff from the previous session:\n"
        f"{handoff_text}\n\n"
        "Continue from here."
    )
