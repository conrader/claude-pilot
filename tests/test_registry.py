import pytest

from claude_pilot import config, registry


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PILOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PILOT_CONFIG", str(tmp_path / "nope.json"))
    config.reload()
    yield


def _rec(name, cwd, state="active", **kw):
    r = {
        "name": name, "cwd": cwd, "session_id": "abc", "goal": "do stuff",
        "state": state, "started_at": registry.iso(registry.now()),
        "active_since": registry.iso(registry.now()),
        "deadline": registry.iso(registry.now() + __import__("datetime").timedelta(hours=1)),
        "max_ticks": 40, "ticks": 0, "history": [], "persistent": False,
        "quiet_ticks": 0, "previous_sessions": [], "compactions": 0,
        "last_estimate": 0,
    }
    r.update(kw)
    return r


def test_load_missing_returns_none(tmp_path):
    path, rec = registry.load("nope")
    assert rec is None
    assert path.name == "nope.json"


def test_save_and_load_round_trip():
    rec = _rec("proj", "/x/proj")
    registry.save(rec)
    _, loaded = registry.load("proj")
    assert loaded["goal"] == "do stuff"
    assert loaded["name"] == "proj"


def test_name_for_cwd_basic():
    assert registry.name_for_cwd("/home/u/My Cool App!") == "my-cool-app"


def test_name_for_cwd_collision_gets_suffix():
    registry.save(_rec("myapp", "/home/u/myapp-one"))
    name = registry.name_for_cwd("/home/u/myapp-two")
    # basename of both dirs differs so no collision expected here; force one:
    registry.save(_rec("dup", "/a/dup"))
    other = registry.name_for_cwd("/b/dup")
    assert other != "dup"
    assert other.startswith("dup-")


def test_active_names_excludes_expired_non_persistent():
    import datetime
    past = registry.iso(registry.now() - datetime.timedelta(hours=1))
    registry.save(_rec("stale", "/x/stale", deadline=past))
    registry.save(_rec("fresh", "/x/fresh"))
    active = registry.active_names()
    assert "fresh" in active
    assert "stale" not in active


def test_active_names_persistent_ignores_deadline():
    import datetime
    past = registry.iso(registry.now() - datetime.timedelta(hours=1))
    registry.save(_rec("evergreen", "/x/evergreen", deadline=past, persistent=True))
    assert "evergreen" in registry.active_names()


def test_cap_block_within_cap():
    registry.save(_rec("a", "/x/a"))
    assert registry.cap_block("b", allow_more=False, cap=3) == ""


def test_cap_block_at_cap():
    registry.save(_rec("a", "/x/a"))
    registry.save(_rec("b", "/x/b"))
    registry.save(_rec("c", "/x/c"))
    msg = registry.cap_block("d", allow_more=False, cap=3)
    assert msg
    assert "cap is 3" in msg


def test_cap_block_allow_more_bypasses():
    registry.save(_rec("a", "/x/a"))
    registry.save(_rec("b", "/x/b"))
    registry.save(_rec("c", "/x/c"))
    assert registry.cap_block("d", allow_more=True, cap=3) == ""


def test_find_by_cwd_exact_and_ancestor(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    registry.save(_rec("proj", str(proj)))
    found = registry.find_by_cwd(str(proj))
    assert found["name"] == "proj"
    sub = proj / "sub"
    sub.mkdir()
    found2 = registry.find_by_cwd(str(sub))
    assert found2["name"] == "proj"


def test_inbox_add_pending_drain_round_trip():
    registry.inbox_add("p", "do the thing")
    registry.inbox_add("p", "do another thing")
    pending = registry.inbox_pending("p")
    assert len(pending) == 2
    assert pending[0]["text"] == "do the thing"

    drained = registry.inbox_drain("p")
    assert len(drained) == 2
    assert registry.inbox_pending("p") == []

    # a second drain finds nothing new
    assert registry.inbox_drain("p") == []
