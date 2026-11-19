# Version: 0.3.3
"""The network screen's routes (NEH-253), over app.core.network.

GET    /network/status              what the unit is connected to
GET    /network/wifi                the Wi-Fi networks in range and the saved ones
POST   /network/wifi/join           start a join (202); its outcome is read from:
GET    /network/wifi/join           the last join's state
DELETE /network/wifi/saved/{uuid}   forget a saved Wi-Fi network (204)

Without nmcli every route answers 501. A read that NetworkManager does not
answer is a 503 with the problem `network_manager_unavailable`. A refusal is
a 409 whose detail lists {code, message} problems, as app.core.unit_mode's
ModeRefused does; the frontend translates by code. `not_permitted` (join, forget)
means polkit does not let the backend activate or save connections; GET
/network/status reports the three permissions. A scan is never refused for
lack of the scan permission: it answers from the cached list.

A request that fails validation answers 422 with each error's `loc`,
`type` and `msg` only (_NetworkRoute): FastAPI's default body echoes the
submitted values, a password among them.

A join changes the link the request may have arrived on, so the request
does not wait for it: the join runs on a daemon thread and the client polls
GET /network/wifi/join.
"""

import threading
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.core import network
from app.core.audit import log_event
from app.models.user import User

def _plain_errors(errors) -> list[dict]:
    """Validation errors as {loc, type, msg}: no `input` or `ctx`, no
    unknown field's name in `loc`, and a `msg` that would repeat a submitted
    value is replaced by the type."""
    plain = []
    for error in errors:
        msg = str(error.get("msg", ""))
        submitted = error.get("input")
        if isinstance(submitted, (str, int, float)) and str(submitted) and str(submitted) in msg:
            msg = str(error.get("type", "value_error"))
        loc = list(error.get("loc", ()))
        if error.get("type") == "extra_forbidden" and len(loc) > 1:
            # The last element is the unknown field's name, which the client chose.
            loc = loc[:-1]
        plain.append({"loc": loc, "type": error.get("type"), "msg": msg})
    return plain


class _NetworkRoute(APIRoute):
    """Answers a validation error on a /network route without echoing the request."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": _plain_errors(exc.errors())})

        return route_handler


router = APIRouter(route_class=_NetworkRoute)

allow_network_read = RoleChecker(["admin", "operator", "reviewer"])   # status, scan, join state
allow_network_write = RoleChecker(["admin", "operator"])               # join, forget


def _require_nmcli() -> None:
    if not network.available():
        raise HTTPException(status_code=501, detail=network.NOT_AVAILABLE)


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={
        "message": "NetworkManager is not answering.",
        "problems": [{"code": "network_manager_unavailable"}],
    })


def _refused(exc: network.NetworkRefused) -> HTTPException:
    return HTTPException(status_code=409, detail=exc.detail())


def _start(target, kwargs: dict) -> None:
    """Run the join on a daemon thread; replaced in the tests to run inline."""
    threading.Thread(target=target, kwargs=kwargs, daemon=True, name="network-join").start()


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssid: str
    security: Literal["open", "password", "enterprise"]
    password: Optional[str] = None
    identity: Optional[str] = None
    method: Optional[Literal["peap", "ttls"]] = None
    innerMethod: Optional[Literal["mschapv2", "pap"]] = None
    anonymousIdentity: Optional[str] = None
    serverName: Optional[str] = None


@router.get("/status")
def get_status(current_user: User = Depends(allow_network_read)):
    """The unit's hostname, the Wi-Fi device, the backend's permissions
    (`{scan, control, modify}`), each Ethernet and Wi-Fi interface, and the
    problems found (app.core.network.status). `gateway` is the IPv4 gateway;
    `dns` lists IPv4 servers, then IPv6. NetworkManager's connectivity value
    is not reported. 503 when any read fails."""
    _require_nmcli()
    try:
        return network.status()
    except network.NetworkUnavailable:
        raise _unavailable()


@router.get("/wifi")
def get_wifi(rescan: bool = Query(default=False), current_user: User = Depends(allow_network_read)):
    """The networks in range (one per SSID, strongest first, the one in use
    at the top), the saved Wi-Fi connections, and `rescanned`.

    Without `rescan` the answer comes at once from NetworkManager's recent
    scan; the client asks with `rescan=true` when the person presses the
    refresh control, which waits for a fresh scan (about 10 s). Without the
    wifi.scan permission the cached list is returned and `rescan` is not
    honoured; `rescanned` says whether a fresh scan was requested for this
    answer. 409 no_wifi_device."""
    _require_nmcli()
    try:
        return network.scan(rescan=rescan)
    except network.NetworkRefused as exc:
        raise _refused(exc)
    except network.NetworkUnavailable:
        raise _unavailable()


@router.get("/wifi/join")
def get_join(current_user: User = Depends(allow_network_read)):
    """The last join: idle, joining, joined or failed, with its problem and
    `restored`: after a failed join, whether the earlier connection is
    active or being activated again (true does not mean its link is up
    yet); null when there was no earlier connection or the join succeeded."""
    _require_nmcli()
    return network.join_state()


@router.post("/wifi/join", status_code=202)
def post_join(
    payload: JoinRequest,
    current_user: User = Depends(allow_network_write),
    db: Session = Depends(get_db_dependency),
):
    """Start joining a Wi-Fi network; 202 with the join state.

    409 refusals, checked before anything runs: ssid_invalid,
    password_required, password_invalid (a line break or NUL; for a
    WPA2/WPA3 password, any ASCII control character), password_length,
    identity_required, identity_invalid, anonymous_identity_invalid,
    method_required, server_name_required, server_name_invalid,
    inner_method_invalid, no_wifi_device, not_permitted, join_in_progress. The audit entry holds the SSID and the
    security kind only."""
    _require_nmcli()
    fields = dict(
        ssid=payload.ssid, security=payload.security, password=payload.password, identity=payload.identity,
        method=payload.method, inner_method=payload.innerMethod,
        anonymous_identity=payload.anonymousIdentity, server_name=payload.serverName,
    )
    try:
        device = network.claim_join(**fields)
    except network.NetworkRefused as exc:
        raise _refused(exc)
    except network.NetworkUnavailable:
        raise _unavailable()
    log_event(db, level="INFO", category="system", action="network_join", actor=current_user.username,
              subject=payload.ssid, detail=f"security={payload.security}")
    try:
        _start(network.run_join, dict(device=device, **fields))
    except Exception:
        network.finish_join(network.JoinOutcome(joined=False, problem={"code": "failed", "detail": None,
                                                                       "server": None}))
        raise
    return JSONResponse(status_code=202, content=network.join_state())


@router.delete("/wifi/saved/{uuid}", status_code=204)
def delete_saved(
    uuid: UUID,
    force: bool = Query(default=False),
    current_user: User = Depends(allow_network_write),
    db: Session = Depends(get_db_dependency),
):
    """Forget a saved Wi-Fi connection. 404 when no saved Wi-Fi connection
    has this UUID; 409 connection_active when it is in use and force is not
    true; 409 not_permitted when polkit refuses the deletion."""
    _require_nmcli()
    try:
        ssid = network.forget(str(uuid), force=force)
    except network.SavedNotFound:
        raise HTTPException(status_code=404, detail="No saved Wi-Fi network has this identifier.")
    except network.NetworkRefused as exc:
        raise _refused(exc)
    except network.NetworkUnavailable:
        raise _unavailable()
    log_event(db, level="INFO", category="system", action="network_forget", actor=current_user.username,
              subject=ssid)
    return Response(status_code=204)
