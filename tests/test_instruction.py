from claude_pilot import instruction


def _rec(**kw):
    r = {"goal": "ship the feature", "ticks": 2, "max_ticks": 40,
         "deadline": "2026-09-14T12:00:00+00:00"}
    r.update(kw)
    return r


def test_build_instruction_contains_goal_and_ticks():
    text = instruction.build_instruction(_rec(), [])
    assert "ship the feature" in text
    assert "tick 3 of 40" in text


def test_build_instruction_contains_markers():
    text = instruction.build_instruction(_rec(), [])
    assert instruction.DONE in text
    assert instruction.BLOCKED in text


def test_build_instruction_includes_queued_instructions():
    queued = [{"text": "also fix the docs"}, {"text": "and add tests"}]
    text = instruction.build_instruction(_rec(), queued)
    assert "also fix the docs" in text
    assert "and add tests" in text
    assert "Instructions from your operator" in text


def test_build_instruction_no_queue_block_when_empty():
    text = instruction.build_instruction(_rec(), [])
    assert "Instructions from your operator" not in text


def test_handoff_request_is_nonempty_string():
    assert isinstance(instruction.handoff_request(), str)
    assert len(instruction.handoff_request()) > 10


def test_handoff_opening_includes_goal_and_handoff_text():
    text = instruction.handoff_opening("previous state notes", _rec())
    assert "ship the feature" in text
    assert "previous state notes" in text
