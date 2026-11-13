"""evaluate_setup: whether the operator must set or confirm the camera roles.

A pure function over the device rows, the stored role records and the pair
last confirmed together. Each reason code has its own test, and so does the
case in which nothing needs doing.

Version: 0.1.0-beta
"""

import copy

import pytest

from capture.camera_roles import evaluate_setup


A = "canoneosrebelt7_1111111"
B = "canoneosrebelt7_2222222"
C = "canoneosrebelt7_3333333"


def _device(hw_id, index, usb_port=None, **extra):
    row = {"hardware_id": hw_id, "index": index, "usb_port": usb_port}
    row.update(extra)
    return row


def _confirmed(role, port=None):
    return {
        "role": role,
        "role_confirmed_at": "2026-11-12T10:00:00+00:00",
        "usb_port_at_confirmation": port,
    }


def _codes(result):
    return [reason["code"] for reason in result["reasons"]]


def _reason(result, code):
    matches = [reason for reason in result["reasons"] if reason["code"] == code]
    assert len(matches) == 1, result
    return matches[0]


@pytest.mark.unit
def test_a_confirmed_pair_on_its_own_ports_needs_no_setup():
    devices = [_device(A, 0, "1-1.2"), _device(B, 1, "1-1.3")]
    records = {A: _confirmed("left", "1-1.2"), B: _confirmed("right", "1-1.3")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result == {"needs_setup": False, "reasons": []}


@pytest.mark.unit
def test_a_body_with_no_record_is_unconfirmed():
    devices = [_device(A, 0), _device(B, 1)]
    records = {A: _confirmed("left")}

    result = evaluate_setup(devices, records, None)

    assert result["needs_setup"] is True
    assert _codes(result) == ["role_unconfirmed"]
    assert _reason(result, "role_unconfirmed")["hardware_ids"] == [B]


@pytest.mark.unit
@pytest.mark.parametrize("record", [
    {"role": None, "role_confirmed_at": None, "usb_port_at_confirmation": None},
    {"role": "left", "role_confirmed_at": None, "usb_port_at_confirmation": None},
    {"role": "middle", "role_confirmed_at": "2026-11-12", "usb_port_at_confirmation": None},
    {},
])
def test_a_role_without_a_confirmation_is_unconfirmed(record):
    result = evaluate_setup([_device(A, 0)], {A: record}, None)

    assert _codes(result) == ["role_unconfirmed"]
    assert _reason(result, "role_unconfirmed")["hardware_ids"] == [A]


@pytest.mark.unit
def test_two_bodies_with_one_confirmed_role_conflict():
    devices = [_device(A, 0), _device(B, 1)]
    records = {A: _confirmed("left"), B: _confirmed("left")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result["needs_setup"] is True
    assert _codes(result) == ["role_conflict"]
    assert _reason(result, "role_conflict")["hardware_ids"] == [A, B]


@pytest.mark.unit
def test_a_body_outside_the_confirmed_pair_changes_the_pair():
    devices = [_device(A, 0), _device(C, 1)]
    records = {A: _confirmed("left"), C: _confirmed("right")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result["needs_setup"] is True
    assert _codes(result) == ["pair_changed"]
    assert _reason(result, "pair_changed")["hardware_ids"] == [B, C]


@pytest.mark.unit
def test_one_body_of_the_pair_missing_changes_the_pair():
    devices = [_device(A, 0)]
    records = {A: _confirmed("left"), B: _confirmed("right")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert _codes(result) == ["pair_changed"]
    assert _reason(result, "pair_changed")["hardware_ids"] == [B]


@pytest.mark.unit
def test_no_confirmed_pair_is_not_a_changed_pair():
    devices = [_device(A, 0), _device(B, 1)]
    records = {A: _confirmed("left"), B: _confirmed("right")}

    result = evaluate_setup(devices, records, None)

    assert result == {"needs_setup": False, "reasons": []}


@pytest.mark.unit
def test_a_confirmed_body_on_another_socket_has_changed_port():
    devices = [_device(A, 0, "1-1.3"), _device(B, 1, "1-1.3")]
    records = {A: _confirmed("left", "1-1.2"), B: _confirmed("right", "1-1.3")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result["needs_setup"] is True
    assert _codes(result) == ["port_changed"]
    assert _reason(result, "port_changed")["hardware_ids"] == [A]


@pytest.mark.unit
@pytest.mark.parametrize("now, then", [(None, "1-1.2"), ("1-1.3", None), (None, None)])
def test_an_unknown_port_on_either_side_is_not_a_changed_port(now, then):
    devices = [_device(A, 0, now), _device(B, 1, "1-1.3")]
    records = {A: _confirmed("left", then), B: _confirmed("right", "1-1.3")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result == {"needs_setup": False, "reasons": []}


@pytest.mark.unit
@pytest.mark.parametrize("device", [
    _device(None, 0),
    _device("", 0),
    _device("canon325b_idx0", 0, provisional=True),
    _device("canoneosrebelt7_idx0", 0),
])
def test_a_body_with_no_hardware_identity_is_reported(device):
    result = evaluate_setup([device], {}, None)

    assert result["needs_setup"] is True
    assert _codes(result) == ["identity_missing"]
    assert _reason(result, "identity_missing")["hardware_ids"] == [device["hardware_id"]]


@pytest.mark.unit
def test_every_reason_is_reported_together_in_a_fixed_order():
    devices = [
        _device("canoneosrebelt7_idx0", 0),
        _device(A, 1, "1-1.4"),
        _device(B, 2),
        _device(C, 3),
    ]
    records = {
        A: _confirmed("left", "1-1.2"),
        B: _confirmed("left"),
    }

    result = evaluate_setup(devices, records, {"left": A, "right": "gone"})

    assert _codes(result) == [
        "identity_missing", "role_unconfirmed", "role_conflict",
        "pair_changed", "port_changed",
    ]
    assert _reason(result, "role_unconfirmed")["hardware_ids"] == [C]
    assert _reason(result, "role_conflict")["hardware_ids"] == [A, B]
    assert _reason(result, "pair_changed")["hardware_ids"] == [B, C, "gone"]
    assert _reason(result, "port_changed")["hardware_ids"] == [A]


@pytest.mark.unit
def test_on_a_unit_where_nothing_has_been_confirmed_every_body_is_unconfirmed():
    devices = [_device(A, 0, "1-1.2"), _device(B, 1, "1-1.3")]

    result = evaluate_setup(devices, {}, None)

    assert result == {
        "needs_setup": True,
        "reasons": [{"code": "role_unconfirmed", "hardware_ids": [A, B]}],
    }


@pytest.mark.unit
def test_no_connected_bodies_and_no_pair_needs_no_setup():
    assert evaluate_setup([], {}, None) == {"needs_setup": False, "reasons": []}


@pytest.mark.unit
def test_the_inputs_are_not_changed():
    devices = [_device(A, 0, "1-1.2"), _device(B, 1)]
    records = {A: _confirmed("left", "1-1.1")}
    pair = {"left": A, "right": C}
    before = copy.deepcopy((devices, records, pair))

    evaluate_setup(devices, records, pair)

    assert (devices, records, pair) == before


@pytest.mark.unit
def test_records_may_be_none():
    result = evaluate_setup([_device(A, 0)], None, None)

    assert _codes(result) == ["role_unconfirmed"]


@pytest.mark.unit
def test_two_bodies_sharing_an_identity_are_reported_as_ambiguous():
    devices = [
        _device(A, 0, "1-1.2", identity_ambiguous=True),
        _device(A, 1, "1-1.3", identity_ambiguous=True),
    ]
    records = {A: _confirmed("left", "1-1.2")}

    result = evaluate_setup(devices, records, None)

    assert result["needs_setup"] is True
    assert _codes(result) == ["identity_ambiguous"]
    assert _reason(result, "identity_ambiguous")["hardware_ids"] == [A]


@pytest.mark.unit
def test_an_ambiguous_body_does_not_hide_a_healthy_confirmed_one():
    devices = [
        _device(A, 0, "1-1.2", identity_ambiguous=True),
        _device(B, 1, "1-1.4", identity_ambiguous=False),
    ]
    records = {A: _confirmed("left", "1-1.2"), B: _confirmed("right", "1-1.3")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert _codes(result) == ["identity_ambiguous", "pair_changed", "port_changed"]
    assert _reason(result, "identity_ambiguous")["hardware_ids"] == [A]
    assert _reason(result, "pair_changed")["hardware_ids"] == [A]
    assert _reason(result, "port_changed")["hardware_ids"] == [B]


@pytest.mark.unit
def test_a_row_without_the_ambiguity_key_is_unaffected():
    devices = [_device(A, 0, "1-1.2"), _device(B, 1, "1-1.3")]
    records = {A: _confirmed("left", "1-1.2"), B: _confirmed("right", "1-1.3")}

    result = evaluate_setup(devices, records, {"left": A, "right": B})

    assert result == {"needs_setup": False, "reasons": []}


@pytest.mark.unit
def test_identity_ambiguous_follows_identity_missing_in_the_order():
    devices = [
        _device("canoneosrebelt7_idx0", 0),
        _device(A, 1, identity_ambiguous=True),
        _device(A, 2, identity_ambiguous=True),
        _device(B, 3),
    ]

    result = evaluate_setup(devices, {}, None)

    assert _codes(result) == ["identity_missing", "identity_ambiguous", "role_unconfirmed"]
