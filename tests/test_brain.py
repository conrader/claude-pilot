"""claude_pilot.brain: the connector never breaks a tick."""
from claude_pilot import brain


def test_no_commands_means_no_context_and_ok():
    assert brain.fetch_context({"name": "x"}, {}) == ""
    assert brain.run_judge({"name": "x"}, "reply", {}) == {"verdict": "ok"}


def test_context_gets_the_record_and_returns_stdout():
    out = brain.fetch_context({"name": "x", "goal": "g"}, {"context_command": "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"goal\"].upper())'"})
    assert out == "G"


def test_judge_pause_and_garbage(capsys):
    assert brain.run_judge({}, "r", {"judge_command": "echo '{\"verdict\": \"pause\", \"reason\": \"off goal\"}'"}) == {"verdict": "pause", "reason": "off goal"}
    assert brain.run_judge({}, "r", {"judge_command": "echo not-json"}) == {"verdict": "ok"}
    assert brain.run_judge({}, "r", {"judge_command": "exit 3"}) == {"verdict": "ok"}
    assert "treating as ok" in capsys.readouterr().out
