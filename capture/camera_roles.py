"""Camera roles: which page each body photographs, and whether that is settled.

A camera's role is the page it photographs, "left" or "right". The operator
sets and confirms it per body, and the unit stores it in the camera registry
under the body's hardware id. This module only evaluates what is stored
against what is connected; it reads no file and talks to no camera.

The record evaluate_setup reads, per hardware id:

    {
        "role": "left" | "right" | None,
        "role_confirmed_at": str | None,       # ISO timestamp of the confirmation
        "usb_port_at_confirmation": str | None,  # usb_topology path at that time
    }

and the pair last confirmed together, as {"left": hw_id, "right": hw_id} or
None when no pair has been confirmed.

Version: 0.1.0-beta
"""

import re

ROLES = ("left", "right")

# The reason codes, in the order evaluate_setup reports them.
IDENTITY_MISSING = "identity_missing"
ROLE_UNCONFIRMED = "role_unconfirmed"
ROLE_CONFLICT = "role_conflict"
PAIR_CHANGED = "pair_changed"
PORT_CHANGED = "port_changed"

REASON_CODES = (
    IDENTITY_MISSING,
    ROLE_UNCONFIRMED,
    ROLE_CONFLICT,
    PAIR_CHANGED,
    PORT_CHANGED,
)

# The id a backend lists a body under when the body has no identity of its
# own: "<model slug>_idx<index>". The gphoto2, CHDK and picamera2 paths all
# build it this way.
_PLACEHOLDER_RE = re.compile(r".+_idx\d+")


def has_identity(device):
    """True when the row names a body by an identity of its own.

    A row has none when its hardware id is empty, when the backend marks it
    provisional, or when the id is the per-index placeholder.
    """
    hw_id = device.get("hardware_id")
    if not hw_id or device.get("provisional"):
        return False
    return not _PLACEHOLDER_RE.fullmatch(str(hw_id))


def confirmed_role(record):
    """The record's role when it has been confirmed, else None."""
    if not record:
        return None
    role = record.get("role")
    if role in ROLES and record.get("role_confirmed_at"):
        return role
    return None


def evaluate_setup(devices, records, confirmed_pair):
    """Say whether the operator must set or confirm the camera roles, and why.

    Args:
        devices: The connected bodies, as backend device rows. Each is read
            for "hardware_id", "provisional" and "usb_port".
        records: Role records keyed by hardware id, in the shape this
            module's docstring gives. A missing id, or None, means no record.
        confirmed_pair: {"left": hw_id, "right": hw_id}, or None.

    Returns:
        {"needs_setup": bool, "reasons": [{"code": str, "hardware_ids": [...]}]}.
        The reasons come in REASON_CODES order, each at most once, with its
        hardware ids sorted. needs_setup is True exactly when a reason is
        given.

    A body without an identity is reported as identity_missing and takes no
    part in the other checks, because no role can be stored against it.
    pair_changed is reported only when a pair has been confirmed; its ids are
    the connected bodies outside the pair and the pair's bodies that are not
    connected. port_changed is skipped for a body when either its current
    port or its port at confirmation is None.
    """
    records = records or {}
    found = {code: set() for code in REASON_CODES}

    identified = []
    for device in devices:
        if has_identity(device):
            identified.append(device)
        else:
            found[IDENTITY_MISSING].add(device.get("hardware_id"))

    holders = {role: set() for role in ROLES}
    for device in identified:
        hw_id = device["hardware_id"]
        record = records.get(hw_id) or {}
        role = confirmed_role(record)
        if role is None:
            found[ROLE_UNCONFIRMED].add(hw_id)
            continue
        holders[role].add(hw_id)
        now = device.get("usb_port")
        then = record.get("usb_port_at_confirmation")
        if now is not None and then is not None and now != then:
            found[PORT_CHANGED].add(hw_id)

    for sharing in holders.values():
        if len(sharing) > 1:
            found[ROLE_CONFLICT].update(sharing)

    if confirmed_pair is not None:
        pair = {confirmed_pair.get(role) for role in ROLES}
        pair.discard(None)
        connected = {device["hardware_id"] for device in identified}
        found[PAIR_CHANGED].update(pair ^ connected)

    reasons = [
        {"code": code, "hardware_ids": sorted(found[code], key=_sort_key)}
        for code in REASON_CODES
        if found[code]
    ]
    return {"needs_setup": bool(reasons), "reasons": reasons}


def _sort_key(hw_id):
    # A row's hardware id can be None; it sorts first rather than raising.
    return (hw_id is not None, str(hw_id))
