"""Validation errors as the API answers them: nothing taken from the request.

Errors raised at the request-validation boundary carry `type`, `loc` and
`msg` and no submitted value (app.main's handler for RequestValidationError
answers every router with plain_errors). FastAPI's default 422 body would
carry the `input` that failed, a password or a whole request body, and a
`ctx` that can repeat it; plain_errors drops both, drops an unknown field's
name from `loc`, since the client chose it, and replaces a `msg` that
repeats a submitted value with the error's type. No validator of this
application puts the value it checks in its message; the replacement covers
pydantic's own messages and any later validator.

A route's own HTTPException message may name a non-credential parameter it
refused (for example an unknown time zone or a resolution). No credential,
hash or token is ever placed in a response or a log line.
"""


def plain_errors(errors) -> list[dict]:
    """Validation errors as {type, loc, msg}: no `input` or `ctx`, no unknown field's name, no submitted value."""
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
        plain.append({"type": error.get("type"), "loc": loc, "msg": msg})
    return plain
