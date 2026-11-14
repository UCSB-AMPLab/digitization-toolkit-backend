"""Restarting the backend after a link (NEH-345): the helper's restart-backend verb, and the fallback when it is missing.

The helper and sudo are never run: subprocess.run is replaced, and the
helper is a file in a temporary directory.

    python -m pytest tests/unit/test_system_restart.py

Version: 0.3.3
"""

import subprocess

import pytest


@pytest.fixture
def helper(monkeypatch, tmp_path):
    from app.core import system_helper

    path = tmp_path / "dtk-system-helper"
    monkeypatch.setattr(system_helper, "HELPER", str(path))
    monkeypatch.setattr(system_helper.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, run.returncode, "", "")

    run.returncode = 0
    monkeypatch.setattr(system_helper.subprocess, "run", run)
    from app.core import unit_activity

    # A successful restart keeps its reservation; each test starts and ends without one.
    unit_activity.release_restart()
    yield path, calls, run
    unit_activity.release_restart()


WITH_VERB = """#!/bin/bash
case "$verb" in
    reboot)           cmd_reboot "$@" ;;
    restart-backend)  cmd_restart_backend "$@" ;;
esac
"""
WITHOUT_VERB = """#!/bin/bash
case "$verb" in
    reboot)           cmd_reboot "$@" ;;
esac
"""


def test_helper_with_the_verb_restarts(helper):
    from app.core import fisqua_pairing_policy

    path, calls, _ = helper
    path.write_text(WITH_VERB)
    assert fisqua_pairing_policy.restart_after_link() == "restarting"
    argv, kwargs = calls[0]
    assert argv == ["sudo", str(path), "restart-backend"]
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_helper_without_the_verb_is_restart_needed_and_not_run(helper):
    from app.core import fisqua_pairing_policy

    path, calls, _ = helper
    path.write_text(WITHOUT_VERB)
    assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
    assert calls == []


def test_no_helper_is_restart_needed(helper):
    from app.core import fisqua_pairing_policy

    _, calls, _ = helper
    assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
    assert calls == []


def test_helper_that_fails_is_restart_needed(helper):
    from app.core import fisqua_pairing_policy

    path, calls, run = helper
    path.write_text(WITH_VERB)
    run.returncode = 1
    assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
    assert len(calls) == 1


def test_capture_in_progress_is_restart_needed(helper):
    from app.core import unit_activity, fisqua_pairing_policy

    path, calls, _ = helper
    path.write_text(WITH_VERB)
    with unit_activity.tracking():
        assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
    assert calls == []


def test_power_route_still_uses_the_same_helper_path():
    from app.api import system
    from app.core import system_helper

    assert system.HELPER == system_helper.HELPER


# ---------------------------------------------------------------------------
# A capture and the restart are decided under one lock
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_reservation():
    from app.core import unit_activity

    unit_activity.release_restart()
    yield
    unit_activity.release_restart()


def _enter_tracked():
    """Enter the capture routes' dependency as FastAPI would; returns the generator to close."""
    from app.core import unit_activity

    gen = unit_activity.tracked()
    next(gen)
    return gen


def test_a_capture_first_prevents_the_restart(helper, clean_reservation):
    import threading

    from app.core import unit_activity, fisqua_pairing_policy

    path, calls, _ = helper
    path.write_text(WITH_VERB)
    inside, finish = threading.Event(), threading.Event()

    def capture():
        with unit_activity.tracking():
            inside.set()
            finish.wait(2)

    worker = threading.Thread(target=capture)
    worker.start()
    try:
        assert inside.wait(2)
        assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
        assert calls == []
        assert not unit_activity.restart_pending()
    finally:
        finish.set()
        worker.join(2)


def test_a_pending_restart_refuses_a_new_capture(helper, clean_reservation, monkeypatch):
    import threading

    from fastapi import HTTPException

    from app.core import unit_activity, fisqua_pairing_policy, system_helper

    path, calls, _ = helper
    path.write_text(WITH_VERB)
    in_helper, release = threading.Event(), threading.Event()

    def slow_run(verb):
        in_helper.set()
        release.wait(2)
        return True

    monkeypatch.setattr(system_helper, "run", slow_run)
    result = []
    worker = threading.Thread(target=lambda: result.append(fisqua_pairing_policy.restart_after_link()))
    worker.start()
    try:
        assert in_helper.wait(2)
        with pytest.raises(HTTPException) as refused:
            _enter_tracked()
        assert refused.value.status_code == 503
        assert refused.value.detail["code"] == "backend_restarting"
        assert int(refused.value.headers["Retry-After"]) > 0
        assert not unit_activity.in_progress()
    finally:
        release.set()
        worker.join(2)
    assert result == ["restarting"]
    assert unit_activity.restart_pending()


@pytest.mark.parametrize("setup", ["no_verb", "fails", "raises"])
def test_reservation_released_when_the_restart_does_not_happen(helper, clean_reservation, monkeypatch, setup):
    from app.core import unit_activity, fisqua_pairing_policy, system_helper

    path, calls, run = helper
    path.write_text(WITHOUT_VERB if setup == "no_verb" else WITH_VERB)
    if setup == "fails":
        run.returncode = 1
    if setup == "raises":
        def boom(verb):
            raise RuntimeError("helper exploded")
        monkeypatch.setattr(system_helper, "run", boom)
        with pytest.raises(RuntimeError):
            fisqua_pairing_policy.restart_after_link()
    else:
        assert fisqua_pairing_policy.restart_after_link() == "restart_needed"
    assert not unit_activity.restart_pending()
    _enter_tracked().close()


def test_reservation_expires(clean_reservation, monkeypatch):
    from app.core import unit_activity

    now = [100.0]
    monkeypatch.setattr(unit_activity, "_clock", lambda: now[0])
    assert unit_activity.reserve_restart() is True
    assert unit_activity.reserve_restart() is False
    assert unit_activity.restart_pending()
    now[0] += unit_activity.RESTART_RESERVATION_SECONDS + 1
    assert not unit_activity.restart_pending()


def test_capture_route_answers_503_while_restarting(client, clean_reservation, monkeypatch):
    from app.core import unit_activity
    from tests.unit.test_capture_manifest_export import _as

    assert unit_activity.reserve_restart() is True
    resp = _as(client, "operator").post("/cameras/test-capture/0")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "backend_restarting"
    assert "Retry-After" in resp.headers
