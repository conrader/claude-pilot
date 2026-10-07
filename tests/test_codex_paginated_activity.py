import sqlite3
from pathlib import Path

from claude_pilot.agents import codex


def setup_history(root, cwd, status):
    root.mkdir(parents=True)
    with sqlite3.connect(root/'state_5.sqlite') as db:
        db.execute('create table threads(id text,cwd text,updated_at_ms integer,updated_at integer)')
        db.execute('insert into threads values(?,?,?,?)',('session',str(cwd),125000,125))
    with sqlite3.connect(root/'thread_history_1.sqlite') as db:
        db.execute('create table thread_turns(thread_id text,status text,rollout_ordinal integer)')
        db.execute('insert into thread_turns values(?,?,?)',('session',status,1))


def test_active_paginated_turn_is_live_without_rollout(tmp_path,monkeypatch):
    root=tmp_path/'codex';setup_history(root,tmp_path/'repo','inProgress')
    monkeypatch.setenv('CODEX_HOME',str(root))
    assert codex.newest_transcript_mtime(str(tmp_path/'repo'),'session','') > 125


def test_completed_turn_preserves_actual_timestamp(tmp_path,monkeypatch):
    root=tmp_path/'codex';setup_history(root,tmp_path/'repo','completed')
    monkeypatch.setenv('CODEX_HOME',str(root))
    assert codex.newest_transcript_mtime(str(tmp_path/'repo'),'session','') == 125


def test_other_project_does_not_claim_session_activity(tmp_path,monkeypatch):
    root=tmp_path/'codex';setup_history(root,tmp_path/'other','inProgress')
    monkeypatch.setenv('CODEX_HOME',str(root))
    assert codex.newest_transcript_mtime(str(tmp_path/'repo'),'session','') is None


def test_absent_history_still_returns_no_activity(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'missing'))
    assert codex.newest_transcript_mtime(str(tmp_path/'repo'),'session','') is None


def test_desktop_fallback_queues_into_same_chat_without_exec(tmp_path,monkeypatch):
    from types import SimpleNamespace
    root=tmp_path/'codex';root.mkdir()
    with sqlite3.connect(root/'state_5.sqlite') as db:
        db.execute('create table threads(id text,cwd text,history_mode text)')
        db.execute('insert into threads values(?,?,?)',('session',str(tmp_path/'repo'),'paginated'))
    with sqlite3.connect(root/'thread_history_1.sqlite') as db:
        db.execute('create table thread_turns(thread_id text,status text,rollout_ordinal integer)')
        db.execute('insert into thread_turns values(?,?,?)',('session','completed',1))
    monkeypatch.setenv('CODEX_HOME',str(root));calls=[]
    def transport_run(host,argv,**kwargs):
        calls.append(argv);return SimpleNamespace(returncode=0,stdout='queued',stderr='')
    monkeypatch.setattr(codex.transport,'run',transport_run)
    ok,reply=codex.resume({'cwd':str(tmp_path/'repo'),'session_id':'session'},'Continue the exact goal',{})
    assert ok and 'outcome remains unverified' in reply
    assert calls == [['codex','queue','--thread','session','--message','Continue the exact goal']]
