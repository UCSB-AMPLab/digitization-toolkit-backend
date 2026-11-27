"""Read-only Tailscale status (NEH-254): app.core.tailscale and GET /network/tailscale.

No test runs a real tailscale. tailscale._which is pointed at a fake
executable written into tmp_path: a Python script that records its argv,
its stdin and its PID, then prints the stdout and stderr the test set,
optionally sleeps, floods a stream, or exits non-zero. The status JSON is
synthetic, shaped like `tailscale status --json` from Tailscale 1.104.7.

    python -m pytest tests/unit/test_tailscale.py

Version: 0.4.0
"""

import json
import logging
import os
import subprocess
import sys
import time

import pytest

SECRET_MARKERS = (
    "peer-host", "100.64.0.9", "nodekey:", "someone@example.edu", "Someone",
    "https://login.tailscale.com/a/abc123", "123456789", "987654321", "PeerKey", "MagicDNSEnabled",
    "tskey-", "FutureField",
)

KEYED_HEALTH = [
    "Unable to connect to the Tailscale coordination server to synchronize the state of your tailnet. "
    "Last error: node key regeneration for nodekey:0f1e2d3c4b5a69788796a5b4c3d2e1f0 failed",
    "tailnet lock: nlpub:aa11bb22cc33dd44 rejected; privkey:ee55ff6600112233 nlpriv:99887766 tlpub:44556677",
    "see https://login.tailscale.com/admin?key=nodekey%3A5a5a5a5a5a5a5a5a",
]
# Text that only Tailscale wrote: none of it may reach a log record or an answer.
TAILSCALE_TEXT = ("coordination server", "0f1e2d3c4b5a6978", "aa11bb22cc33dd44", "ee55ff6600112233",
                  "99887766", "44556677", "5a5a5a5a5a5a5a5a", "nodekey", "nlpub", "privkey", "nlpriv",
                  "tlpub", "tailscaled", "tailnet lock")


def _assert_no_tailscale_text_logged(records):
    for record in records:
        text = record.getMessage() + " " + repr(record.args) + " " + str(record.exc_text or "")
        for marker in TAILSCALE_TEXT:
            assert marker not in text, (record.levelname, text)


FAKE_SCRIPT = """#!{python} -I
import json, os, stat, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "spec.json")) as f:
    spec = json.load(f)
mode = os.fstat(0).st_mode
call = {{"argv": sys.argv[1:], "pid": os.getpid(), "stdin_chr": stat.S_ISCHR(mode), "stdin": sys.stdin.read()}}
with open(os.path.join(here, "call.json"), "w") as f:
    json.dump(call, f)
with open(os.path.join(here, "stdout.bin"), "rb") as f:
    sys.stdout.buffer.write(f.read())
sys.stdout.buffer.flush()
with open(os.path.join(here, "stderr.bin"), "rb") as f:
    sys.stderr.buffer.write(f.read())
sys.stderr.buffer.flush()
if spec["flood"]:
    stream = sys.stdout.buffer if spec["flood"] == "stdout" else sys.stderr.buffer
    chunk = b"x" * 65536
    while True:
        stream.write(chunk)
        stream.flush()
        time.sleep(0.01)
time.sleep(spec["sleep"])
sys.exit(spec["rc"])
"""


def _status(state="Running", online=True, self_node=True, health=None):
    """A status document with Peer, User, CurrentTailnet, AuthURL and keys filled in."""
    doc = {
        "Version": "1.104.7-t0000000000-g0000000000",
        "TUN": True,
        "BackendState": state,
        "HaveNodeKey": True,
        "AuthURL": "https://login.tailscale.com/a/abc123",
        "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
        "Health": health if health is not None else [],
        "MagicDNSSuffix": "example-tailnet.ts.net",
        "CurrentTailnet": {"Name": "someone@example.edu", "MagicDNSSuffix": "example-tailnet.ts.net",
                           "MagicDNSEnabled": True},
        "CertDomains": ["captua-01.example-tailnet.ts.net"],
        "Peer": {
            "nodekey:aaaa": {"ID": "123456789", "PublicKey": "nodekey:aaaa", "HostName": "peer-host",
                             "DNSName": "peer-host.example-tailnet.ts.net.",
                             "TailscaleIPs": ["100.64.0.9"], "Online": True, "UserID": 123456789},
        },
        "User": {"123456789": {"ID": 123456789, "LoginName": "someone@example.edu",
                               "DisplayName": "Someone"}},
        "ClientVersion": {"RunningLatest": True},
        "ExtraRecords": None,
    }
    if self_node:
        doc["Self"] = {
            "ID": "987654321", "PublicKey": "nodekey:bbbb", "HostName": "captua-01",
            "DNSName": "captua-01.example-tailnet.ts.net.", "OS": "linux", "UserID": 123456789,
            "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"], "Online": online,
            "PeerKey": "PeerKey-secret",
        }
    return doc


RUNNING_ANSWER = {
    "installed": True,
    "state": "Running",
    "online": True,
    "name": "captua-01.example-tailnet.ts.net",
    "addresses": ["100.101.102.103", "fd7a:115c:a1e0::1"],
    "health": 0,
    "needs_login": False,
}

NOT_INSTALLED = {"installed": False, "state": None, "online": None, "name": None, "addresses": [],
                 "health": 0, "needs_login": None}


class FakeTailscale:
    """The fake executable and the answer it gives on its next run."""

    def __init__(self, directory):
        self.dir = directory
        self.binary = directory / "tailscale"
        self.binary.write_text(FAKE_SCRIPT.format(python=sys.executable), encoding="utf-8")
        self.binary.chmod(0o755)
        self.answer(stdout=json.dumps(_status()))

    def answer(self, *, stdout="", stderr="", rc=0, sleep=0, flood=None):
        out = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        err = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
        (self.dir / "stdout.bin").write_bytes(out)
        (self.dir / "stderr.bin").write_bytes(err)
        (self.dir / "spec.json").write_text(json.dumps({"rc": rc, "sleep": sleep, "flood": flood}))
        return self

    def call(self):
        return json.loads((self.dir / "call.json").read_text())


@pytest.fixture
def ts(tmp_path, monkeypatch):
    from app.core import tailscale
    fake = FakeTailscale(tmp_path)
    monkeypatch.setattr(tailscale, "_which", lambda: str(fake.binary))
    return fake


def _spy_children(monkeypatch):
    """Record every child app.core.tailscale starts."""
    from app.core import tailscale
    children = []
    real = subprocess.Popen

    def spawn(argv, **kwargs):
        child = real(argv, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(tailscale, "_spawn", spawn)
    return children


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_running(ts):
    from app.core import tailscale
    assert tailscale.status() == RUNNING_ANSWER


@pytest.mark.unit
def test_stopped(ts):
    from app.core import tailscale
    ts.answer(stdout=json.dumps(_status(state="Stopped", online=False)))
    answer = tailscale.status()
    assert answer["state"] == "Stopped"
    assert answer["online"] is False
    assert answer["needs_login"] is False


@pytest.mark.unit
def test_needs_login_is_a_flag_and_health_is_a_count(ts):
    from app.core import tailscale
    health = ["You are logged out. The last login error was: none"]
    ts.answer(stdout=json.dumps(_status(state="NeedsLogin", online=False, health=health)))
    answer = tailscale.status()
    assert answer["state"] == "NeedsLogin"
    assert answer["needs_login"] is True
    assert answer["health"] == 1
    assert "logged out" not in json.dumps(answer)


@pytest.mark.unit
def test_no_self_node_and_null_lists(ts):
    from app.core import tailscale
    doc = _status(state="NoState", self_node=False)
    doc["TailscaleIPs"] = None
    doc["Health"] = None
    ts.answer(stdout=json.dumps(doc))
    assert tailscale.status() == {"installed": True, "state": "NoState", "online": None, "name": None,
                                  "addresses": [], "health": 0, "needs_login": False}


@pytest.mark.unit
def test_not_installed(monkeypatch):
    from app.core import tailscale
    monkeypatch.setattr(tailscale, "_which", lambda: None)

    def never(*a, **kw):
        raise AssertionError("nothing runs without the binary")
    monkeypatch.setattr(subprocess, "Popen", never)
    assert tailscale.status() == NOT_INSTALLED


@pytest.mark.unit
def test_fields_of_the_wrong_type_are_not_passed_through(ts):
    from app.core import tailscale
    doc = _status()
    doc["BackendState"] = {"Peer": "peer-host"}
    doc["TailscaleIPs"] = ["100.101.102.103", {"nodekey:aaaa": 1}]
    doc["Health"] = ["a warning", ["nested"]]
    doc["Self"]["Online"] = "yes"
    doc["Self"]["DNSName"] = 42
    ts.answer(stdout=json.dumps(doc))
    assert tailscale.status() == {"installed": True, "state": None, "online": None, "name": None,
                                  "addresses": ["100.101.102.103"], "health": 1, "needs_login": False}


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_nothing_from_peer_user_tailnet_authurl_or_keys_reaches_the_answer(ts):
    from app.core import tailscale
    answer = tailscale.status()
    assert set(answer) == set(RUNNING_ANSWER)
    text = json.dumps(answer)
    for marker in SECRET_MARKERS:
        assert marker not in text


@pytest.mark.unit
def test_unfamiliar_keys_holding_secrets_leave_the_answer_identical(ts):
    from app.core import tailscale
    doc = _status()
    doc["FutureField"] = {"Nested": {"AuthKey": "tskey-auth-kFUTURE-SECRET", "Peers": ["nodekey:cccc"]}}
    doc["Self"]["FutureSelfField"] = {"Secret": "tskey-auth-kSELF-SECRET"}
    ts.answer(stdout=json.dumps(doc))
    answer = tailscale.status()
    assert answer == RUNNING_ANSWER
    assert "SECRET" not in json.dumps(answer)


@pytest.mark.unit
def test_health_text_reaches_neither_the_answer_nor_any_log_record(ts, caplog):
    from app.core import tailscale
    ts.answer(stdout=json.dumps(_status(health=KEYED_HEALTH)))
    with caplog.at_level(logging.DEBUG):
        answer = tailscale.status()
    assert answer["health"] == 3
    for marker in TAILSCALE_TEXT:
        assert marker not in json.dumps(answer)
    _assert_no_tailscale_text_logged(caplog.records)


@pytest.mark.unit
def test_stderr_reaches_no_log_record_and_the_log_states_the_exit_code_and_byte_counts(ts, caplog):
    from app.core import tailscale
    stderr = ("failed to connect to local tailscaled; privkey:ee55ff6600112233 nlpub:aa11bb22cc33dd44 "
              "nodekey%3A5a5a5a5a5a5a5a5a")
    ts.answer(rc=1, stdout="partial", stderr=stderr)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(tailscale.TailscaleUnavailable) as raised:
            tailscale.status()
    assert "tailscaled" not in str(raised.value)
    _assert_no_tailscale_text_logged(caplog.records)
    lines = [r.getMessage() for r in caplog.records if r.name == "app.core.tailscale"]
    assert any("exit code 1" in line and f"stderr {len(stderr)} bytes" in line and "stdout 7 bytes" in line
               for line in lines), lines


@pytest.mark.unit
def test_unparseable_output_is_logged_without_its_text(ts, caplog):
    from app.core import tailscale
    ts.answer(stdout="nlpub:aa11bb22cc33dd44 is not json", stderr="privkey:ee55ff6600112233")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(tailscale.TailscaleUnavailable):
            tailscale.status()
    _assert_no_tailscale_text_logged(caplog.records)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_command_skips_peers_has_no_shell_and_stdin_on_dev_null(ts, monkeypatch):
    from app.core import tailscale
    seen = []
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return real(argv, **kwargs)
    monkeypatch.setattr(tailscale, "_spawn", spy)
    tailscale.status()
    [(argv, kwargs)] = seen
    assert argv == [str(ts.binary), "status", "--json", "--peers=false"]
    assert kwargs.get("shell", False) is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    call = ts.call()
    assert call["argv"] == ["status", "--json", "--peers=false"]
    assert call["stdin_chr"] is True and call["stdin"] == ""


@pytest.mark.unit
def test_the_binary_is_looked_up_on_a_fixed_path(monkeypatch):
    from app.core import tailscale
    seen = {}

    def which(name, path=None):
        seen["name"], seen["path"] = name, path
        return None
    monkeypatch.setattr(tailscale.shutil, "which", which)
    monkeypatch.setenv("PATH", "/tmp/planted")
    assert tailscale._which() is None
    assert seen["name"] == "tailscale"
    assert seen["path"] == tailscale.SEARCH_PATH
    assert "/tmp/planted" not in seen["path"]


@pytest.mark.unit
def test_timeout_is_unavailable_and_the_child_is_reaped(ts, monkeypatch):
    from app.core import tailscale
    monkeypatch.setattr(tailscale, "STATUS_TIMEOUT", 0.5)
    ts.answer(stdout=json.dumps(_status()), sleep=30)
    started = time.monotonic()
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()
    assert time.monotonic() - started < 5
    assert _gone(ts.call()["pid"])


@pytest.mark.unit
def test_a_binary_that_cannot_be_started_is_unavailable(ts):
    from app.core import tailscale
    ts.binary.chmod(0o644)
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ["", "not json at all", "{\"BackendState\": ", "[1, 2, 3]", "null",
                                    "\"Running\"", b"\xff\xfe{}"])
def test_garbage_output_is_unavailable(ts, stdout):
    from app.core import tailscale
    ts.answer(stdout=stdout)
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()


@pytest.mark.unit
def test_an_oserror_from_the_selector_is_unavailable_and_the_child_reaped(ts, monkeypatch, caplog):
    from app.core import tailscale

    class Broken:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def register(self, *a, **kw):
            pass

        def get_map(self):
            return {1: None}

        def select(self, timeout=None):
            raise OSError(9, "Bad file descriptor")
    monkeypatch.setattr(tailscale.selectors, "DefaultSelector", Broken)
    children = _spy_children(monkeypatch)
    ts.answer(stdout=json.dumps(_status()), sleep=30)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(tailscale.TailscaleUnavailable):
            tailscale.status()
    [child] = children
    assert child.returncode is not None and _gone(child.pid)
    assert any("OSError" in r.getMessage() for r in caplog.records if r.name == "app.core.tailscale")


@pytest.mark.unit
def test_an_oserror_from_a_read_is_unavailable_and_the_child_reaped(ts, monkeypatch):
    from app.core import tailscale

    def boom(fd, n):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(tailscale, "_read_chunk", boom, raising=False)
    children = _spy_children(monkeypatch)
    ts.answer(stdout=json.dumps(_status()))
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()
    [child] = children
    assert child.returncode is not None and _gone(child.pid)


def _nested(levels):
    return '{"Health": ' + "[" * levels + "]" * levels + "}"


@pytest.mark.unit
def test_deeply_nested_json_is_unavailable(ts):
    from app.core import tailscale
    ts.answer(stdout=_nested(2000))
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()


@pytest.mark.unit
def test_json_too_deep_for_the_parser_is_unavailable(ts, caplog):
    """20000 levels raise RecursionError inside json.loads on every supported Python."""
    from app.core import tailscale
    ts.answer(stdout=_nested(20000))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(tailscale.TailscaleUnavailable):
            tailscale.status()
    assert any("RecursionError" in r.getMessage() for r in caplog.records if r.name == "app.core.tailscale")


@pytest.mark.unit
def test_json_at_the_depth_limit_is_read(ts):
    from app.core import tailscale
    doc = _status()
    # The top-level object is level 1 and the outermost list of `Deep` level 2: the innermost is at MAX_DEPTH.
    doc["Deep"] = json.loads("[" * (tailscale.MAX_DEPTH - 1) + "]" * (tailscale.MAX_DEPTH - 1))
    ts.answer(stdout=json.dumps(doc))
    assert tailscale.status() == RUNNING_ANSWER


@pytest.mark.unit
def test_a_child_that_cannot_be_reaped_is_given_up_on_and_logged(monkeypatch, caplog):
    from app.core import tailscale

    class Stuck:
        pid = 424242
        returncode = None

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            assert timeout is not None and timeout > 0
            raise subprocess.TimeoutExpired("tailscale", timeout)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="app.core.tailscale"):
        tailscale._stop(Stuck())
    assert time.monotonic() - started < 2
    assert any(r.levelno >= logging.WARNING and "424242" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The output bounds
# ---------------------------------------------------------------------------

def _padded(size):
    """A valid status document of exactly `size` bytes."""
    doc = _status()
    doc["Pad"] = ""
    base = len(json.dumps(doc).encode("utf-8"))
    doc["Pad"] = "x" * (size - base)
    text = json.dumps(doc)
    assert len(text.encode("utf-8")) == size
    return text


@pytest.mark.unit
def test_stdout_at_the_bound_is_read(ts):
    from app.core import tailscale
    ts.answer(stdout=_padded(tailscale.STDOUT_LIMIT))
    assert tailscale.status() == RUNNING_ANSWER


@pytest.mark.unit
def test_stdout_past_the_bound_is_unavailable(ts):
    from app.core import tailscale
    assert tailscale.STDOUT_LIMIT == 256 * 1024
    ts.answer(stdout=_padded(tailscale.STDOUT_LIMIT + 1))
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()


@pytest.mark.unit
def test_stderr_past_the_bound_is_unavailable(ts):
    from app.core import tailscale
    assert tailscale.STDERR_LIMIT == 8 * 1024
    ts.answer(stdout=json.dumps(_status()), stderr="e" * (tailscale.STDERR_LIMIT + 1))
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()


@pytest.mark.unit
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_an_endless_stream_is_cut_off_at_the_bound_and_the_child_killed(ts, monkeypatch, stream):
    from app.core import tailscale
    monkeypatch.setattr(tailscale, "STATUS_TIMEOUT", 10.0)
    ts.answer(stdout="{", flood=stream)
    started = time.monotonic()
    with pytest.raises(tailscale.TailscaleUnavailable):
        tailscale.status()
    assert time.monotonic() - started < 5
    assert _gone(ts.call()["pid"])


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _as(client, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(role, role)
    return client


@pytest.mark.unit
def test_route_200(client, ts):
    resp = _as(client, "reviewer").get("/network/tailscale")
    assert resp.status_code == 200
    assert resp.json() == RUNNING_ANSWER


@pytest.mark.unit
def test_route_not_installed_is_200(client, monkeypatch):
    from app.core import tailscale
    monkeypatch.setattr(tailscale, "_which", lambda: None)
    resp = _as(client, "admin").get("/network/tailscale")
    assert resp.status_code == 200
    assert resp.json() == NOT_INSTALLED


@pytest.mark.unit
def test_route_503_carries_the_problem_code_and_no_stderr(client, ts):
    ts.answer(rc=1, stderr="failed to connect to local tailscaled")
    resp = _as(client, "operator").get("/network/tailscale")
    assert resp.status_code == 503
    assert resp.json()["detail"]["problems"] == [{"code": "tailscale_unavailable"}]
    assert resp.json()["detail"]["message"]
    assert "tailscaled" not in resp.text


@pytest.mark.unit
def test_route_deeply_nested_json_is_503(client, ts):
    ts.answer(stdout=_nested(2000))
    resp = _as(client, "reviewer").get("/network/tailscale")
    assert resp.status_code == 503
    assert resp.json()["detail"]["problems"] == [{"code": "tailscale_unavailable"}]


@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer"])
def test_route_allows_the_network_read_roles(client, ts, role):
    assert _as(client, role).get("/network/tailscale").status_code == 200


@pytest.mark.unit
def test_route_uses_the_network_read_permission():
    import inspect
    from app.api import network as routes

    def checker(handler):
        return inspect.signature(handler).parameters["current_user"].default.dependency
    assert checker(routes.get_tailscale) is routes.allow_network_read
    assert checker(routes.get_status) is routes.allow_network_read


@pytest.mark.unit
def test_route_unauthenticated_is_401(client, ts):
    assert client.get("/network/tailscale").status_code == 401
