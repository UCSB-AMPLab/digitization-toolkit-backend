# Version: 0.4.0
"""The unit's Tailscale status, read-only, through `tailscale status --json` (NEH-254).

The backend runs as user `pi`; tailscaled's socket
(/var/run/tailscale/tailscaled.sock) is world-writable, so the status
command needs no root, no sudo and no helper verb. Nothing here sets up,
logs in, or re-joins a tailnet.

The binary is looked up on SEARCH_PATH, never on the backend's own PATH,
and run by its absolute path as `tailscale status --json --peers=false`: an
argv list, no shell, stdin on /dev/null, LC_ALL=C.UTF-8. The run is bounded
by STATUS_TIMEOUT and by what is read while it runs: at most STDOUT_LIMIT
bytes of stdout and STDERR_LIMIT bytes of stderr. Past either bound, or the
time limit, the child is killed; reaping it waits at most REAP_TIMEOUT, and
a child that is still not reaped then is logged and left.

The answer is built from these values only, never by removing fields from
the parsed document: `installed`, `state` (BackendState), `online`
(Self.Online), `name` (Self.DNSName without its trailing dot), `addresses`
(TailscaleIPs), `health` (how many health warnings there are) and
`needs_login` (BackendState is NeedsLogin). A value of the wrong type is
reported as null, and `addresses` keeps only strings.

No text written by Tailscale (stdout, stderr, health messages) reaches the
answer, an exception message or a log record at any level: a health message
or an error can carry a key. A failure is logged as its kind, the exit
code, and the byte counts of stdout and stderr; an unexpected exception
adds its type name only.

Without the binary the answer has `installed: false`, `health: 0` and null
for the rest. Every failure once the command is started raises
TailscaleUnavailable after the child is stopped: the time limit, a bound, a
non-zero exit, output that is not a UTF-8 JSON object, a document nested
deeper than MAX_DEPTH, or any Exception while collecting or decoding. So
does a command that cannot be started.

This module imports nothing from FastAPI.
"""

import json
import logging
import os
import selectors
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
STATUS_TIMEOUT = 5.0
REAP_TIMEOUT = 5.0
STDOUT_LIMIT = 256 * 1024
STDERR_LIMIT = 8 * 1024
# How deeply the status document may nest; the top-level object is level 1.
MAX_DEPTH = 32
_CHUNK = 64 * 1024

# The seams the tests replace.
_spawn = subprocess.Popen
_read_chunk = os.read
_monotonic = time.monotonic

_UNAVAILABLE = "tailscale status did not answer."


class TailscaleUnavailable(Exception):
    """tailscale is installed but its status could not be read.

    The message never carries anything the command wrote."""


def _which() -> Optional[str]:
    """The absolute path of the tailscale binary on SEARCH_PATH, or None."""
    return shutil.which("tailscale", path=SEARCH_PATH)


def _child_env() -> dict:
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    return env


def _stop(child) -> None:
    """Kill the child if it is still running, and reap it within REAP_TIMEOUT."""
    try:
        if child.poll() is None:
            child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("tailscale status (pid %s) was not reaped within %g s; leaving it.",
                       child.pid, REAP_TIMEOUT)


def _collect(child, buffers: dict) -> Optional[str]:
    """Read the child's stdout and stderr into `buffers` within the bounds and the time limit.

    Returns None when both streams ended within their bounds and the child
    exited before the limit, or else the kind of failure."""
    limits = {"out": STDOUT_LIMIT, "err": STDERR_LIMIT}
    deadline = _monotonic() + STATUS_TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ, "out")
        selector.register(child.stderr, selectors.EVENT_READ, "err")
        while selector.get_map():
            remaining = deadline - _monotonic()
            if remaining <= 0:
                return "timeout"
            for key, _ in selector.select(remaining):
                name = key.data
                # Never more than one byte past the bound.
                chunk = _read_chunk(key.fd, min(_CHUNK, limits[name] + 1 - len(buffers[name])))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[name] += chunk
                if len(buffers[name]) > limits[name]:
                    return "stdout over the bound" if name == "out" else "stderr over the bound"
    try:
        child.wait(timeout=max(0.0, deadline - _monotonic()))
    except subprocess.TimeoutExpired:
        return "timeout"
    return None


def _too_deep(doc) -> bool:
    """Whether any list or object in `doc` sits deeper than MAX_DEPTH (walked without recursion)."""
    stack = [(doc, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if depth > MAX_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _fail(kind: str, child, buffers: dict) -> TailscaleUnavailable:
    """Log the failure's kind, exit code and byte counts; the exception to raise."""
    logger.warning("tailscale status failed: %s; exit code %s; stdout %d bytes; stderr %d bytes",
                   kind, child.returncode, len(buffers["out"]), len(buffers["err"]))
    return TailscaleUnavailable(_UNAVAILABLE)


def _read_status(binary: str) -> dict:
    """The parsed status document; TailscaleUnavailable on any failure."""
    failure = None
    try:
        child = _spawn([binary, "status", "--json", "--peers=false"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env())
    except (OSError, ValueError) as exc:
        failure = f"tailscale status could not be started ({type(exc).__name__})."
    if failure is not None:
        logger.warning(failure)
        raise TailscaleUnavailable(_UNAVAILABLE)

    buffers = {"out": bytearray(), "err": bytearray()}
    try:
        kind = _collect(child, buffers)
    except Exception as exc:
        kind = f"{type(exc).__name__} while reading"
    finally:
        _stop(child)
        for stream in (child.stdout, child.stderr):
            try:
                stream.close()
            except Exception:
                pass
    if kind is None and child.returncode != 0:
        kind = "non-zero exit"
    if kind is None:
        try:
            doc = json.loads(bytes(buffers["out"]).decode("utf-8"))
        except Exception as exc:
            # ValueError (UnicodeDecodeError, JSONDecodeError), RecursionError, ...
            kind = f"{type(exc).__name__} while decoding"
        else:
            if not isinstance(doc, dict):
                kind = "not a JSON object"
            elif _too_deep(doc):
                kind = f"nested deeper than {MAX_DEPTH}"
    if kind is not None:
        raise _fail(kind, child, buffers)
    return doc


def _not_installed() -> dict:
    return {"installed": False, "state": None, "online": None, "name": None, "addresses": [],
            "health": 0, "needs_login": None}


def _string(value) -> Optional[str]:
    return value if isinstance(value, str) else None


def _flag(value) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _name(dns_name) -> Optional[str]:
    if not isinstance(dns_name, str):
        return None
    return dns_name.rstrip(".") or None


def status() -> dict:
    """{installed, state, online, name, addresses, health, needs_login}; see the module docstring."""
    binary = _which()
    if binary is None:
        return _not_installed()
    doc = _read_status(binary)
    node = doc.get("Self")
    if not isinstance(node, dict):
        node = {}
    state = _string(doc.get("BackendState"))
    return {
        "installed": True,
        "state": state,
        "online": _flag(node.get("Online")),
        "name": _name(node.get("DNSName")),
        "addresses": _strings(doc.get("TailscaleIPs")),
        "health": len(_strings(doc.get("Health"))),
        "needs_login": state == "NeedsLogin",
    }
