#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "aiohttp", "pyseventeentrack"]
# ///

import asyncio
import json
import pathlib
import sys

import aiohttp
import requests

# The shared root module holds config/auth resolution, the status map,
# carrier lookup and the account-mode plumbing. It lives at the plugin root
# so that other tools can adopt it the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _17track
from _17track import ToolError

BASE_URL = "https://api.17track.net/track/v1"
# Carrier changes live on the v2.4 surface, like deletetrack in
# remove_package. register and gettrackinfo stay on v1, matching the rest
# of this tool.
CHANGE_CARRIER_URL = "https://api.17track.net/track/v2.4/changecarrier"

# The documented /register error for "Tracking number {0} is already
# registered.". Detected by code only: the similar -18019911 ("the tracking
# number of this carrier can not be registered at the moment") also contains
# the substring "registered" in its message, so message matching is unsafe.
DUPLICATE_REGISTRATION_CODE = -18019901


def _unexpected_response(endpoint: str) -> ToolError:
    """Loud failure for a response shape the code does not understand."""
    return ToolError(
        f"17track {endpoint} returned an unexpected response, so the "
        "result could not be confirmed."
    )


def register_tracking_number(
    tracking_number: str, api_token: str, carrier_code=None, deadline=None
) -> dict:
    """Register a number; report the outcome for the caller to act on.

    Returns {"registered": bool, "corrected_carrier": code or None}.
    "registered" is False when the API rejects the number with the
    duplicate code -18019901 ("already registered"); that is expected
    behaviour, not an error. The caller decides what it means: without a
    carrier it is a no-op, but with one the override must be applied via
    /changecarrier instead.

    When a carrier was supplied and the accepted entry carries a different
    code, the /register docs document this as a correction, not a
    rejection: the system matches the most possible carrier and returns its
    code. The registration has succeeded, so this is reported as success
    with "corrected_carrier" set to the code 17track actually assigned;
    the caller surfaces that code (resolved to a name) plus a note instead
    of discarding the success.

    Everything else fails loudly: an empty or unexpected response, an entry
    for a different number, an entry that omits the assigned carrier when
    one was requested (the assignment cannot be confirmed), or any
    rejection other than the duplicate code. An unconfirmed registration
    must never skip /changecarrier and return stale data.
    """
    item = {"number": tracking_number}
    if carrier_code is not None:
        item["carrier"] = carrier_code
    timeout = (
        _17track.api_timeout_remaining(deadline)
        if deadline is not None
        else _17track.REQUEST_TIMEOUT_SECONDS
    )
    try:
        response = requests.post(
            f"{BASE_URL}/register",
            headers={"17token": api_token},
            json=[item],
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, json.JSONDecodeError) as err:
        raise ToolError(f"Could not register the tracking number: {err}") from err

    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise _unexpected_response("/register")
    data = body["data"]
    accepted = data.get("accepted") or []
    rejected = data.get("rejected") or []
    if not isinstance(accepted, list) or not isinstance(rejected, list):
        raise _unexpected_response("/register")

    for entry in accepted:
        if not isinstance(entry, dict):
            raise _unexpected_response("/register")
        if str(entry.get("number")) != str(tracking_number):
            continue
        if carrier_code is not None:
            entry_carrier = entry.get("carrier")
            if entry_carrier is None or entry_carrier == "":
                # The accepted entry must state which carrier was assigned;
                # without it the requested carrier cannot be confirmed.
                raise ToolError(
                    "17track did not confirm the registration's carrier; "
                    "the tracking number's carrier is unknown."
                )
            if str(entry_carrier) != str(carrier_code):
                # Documented correction: 17track registered the number
                # under a different carrier than the one requested. That is
                # still a successful registration; report the assigned code
                # so the caller can surface it.
                return {"registered": True, "corrected_carrier": entry_carrier}
        return {"registered": True, "corrected_carrier": None}

    for rejection in rejected:
        if not isinstance(rejection, dict):
            raise _unexpected_response("/register")
        if str(rejection.get("number")) != str(tracking_number):
            continue
        error = rejection.get("error")
        if not isinstance(error, dict):
            error = {}
        if error.get("code") == DUPLICATE_REGISTRATION_CODE:
            return {"registered": False, "corrected_carrier": None}
        message = error.get("message") or f"code {error.get('code')}"
        raise ToolError(f"Tracking number rejected: {message}")

    raise ToolError(
        "17track did not confirm the registration; the tracking number's "
        "registration state is unknown."
    )


def change_carrier(
    tracking_number: str, api_token: str, carrier_code: int, deadline=None
) -> None:
    """Override the carrier of an already-registered number (REST v2.4).

    carrier_old is omitted: per the API docs it only matters when a number
    is registered with multiple carriers, and in that case the API refuses
    the change with an explanatory message, which is surfaced below.

    Returns only when an accepted entry confirms both the requested number
    and the requested carrier code; any other outcome fails loudly.
    """
    timeout = (
        _17track.api_timeout_remaining(deadline)
        if deadline is not None
        else _17track.REQUEST_TIMEOUT_SECONDS
    )
    try:
        response = requests.post(
            CHANGE_CARRIER_URL,
            headers={"17token": api_token},
            json=[{"number": tracking_number, "carrier_new": carrier_code}],
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, json.JSONDecodeError) as err:
        raise ToolError(f"Could not change the carrier: {err}") from err

    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise _unexpected_response("/changecarrier")
    data = body["data"]
    accepted = data.get("accepted") or []
    rejected = data.get("rejected") or []
    if not isinstance(accepted, list) or not isinstance(rejected, list):
        raise _unexpected_response("/changecarrier")

    for rejection in rejected:
        if not isinstance(rejection, dict):
            raise _unexpected_response("/changecarrier")
        if str(rejection.get("number")) != str(tracking_number):
            continue
        error = rejection.get("error")
        if not isinstance(error, dict):
            error = {}
        message = error.get("message") or f"code {error.get('code')}"
        raise ToolError(f"17track refused the carrier change: {message}")

    # Success requires an accepted entry matching BOTH the requested number
    # and the requested carrier code. Anything else - an empty object, an
    # entry for a different number or carrier, or no entry at all - is a
    # change the API did not confirm, and one we must not report as done:
    # that is exactly the silent no-op this endpoint exists to fix.
    for entry in accepted:
        if not isinstance(entry, dict):
            raise _unexpected_response("/changecarrier")
        if str(entry.get("number")) == str(tracking_number) and str(
            entry.get("carrier")
        ) == str(carrier_code):
            return

    raise ToolError(
        "17track did not confirm the carrier change; the tracking "
        "number's carrier was not modified."
    )


def get_tracking_info(
    tracking_number: str, api_token: str, deadline=None
) -> dict:
    # After registration, the API may not have indexed the number yet.
    # Retry once after a short delay if the first call returns nothing.
    # The delay is deadline-aware: a request can consume nearly the whole
    # budget, and an unconditional sleep would push the tool past it.
    for attempt in range(2):
        timeout = (
            _17track.api_timeout_remaining(deadline)
            if deadline is not None
            else _17track.REQUEST_TIMEOUT_SECONDS
        )
        try:
            response = requests.post(
                f"{BASE_URL}/gettrackinfo",
                headers={"17token": api_token},
                json=[{"number": tracking_number}],
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, json.JSONDecodeError) as err:
            raise ToolError(f"Could not fetch tracking info: {err}") from err

        accepted = body.get("data", {}).get("accepted", [])
        if accepted:
            return accepted[0]

        if attempt == 0:
            _17track.lookup_retry_sleep(deadline)

    raise ToolError("No tracking info returned for the given number.")


async def track_account_mode(
    tracking_number: str,
    carrier_code,
    carriers: dict | None,
    email: str,
    password: str,
) -> dict:
    timeout = aiohttp.ClientTimeout(total=_17track.REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _17track.account_login(session, email, password)
        await _17track.account_add_tracking_number(session, tracking_number)

        package = await _17track.account_find_package(session, tracking_number)
        if package is None:
            # A freshly added number may not be visible yet; retry once.
            await asyncio.sleep(_17track.LOOKUP_RETRY_SLEEP_SECONDS)
            package = await _17track.account_find_package(session, tracking_number)
        if package is None:
            raise ToolError(
                f"Tracking number {tracking_number} was added but could not "
                "be found in the account's package list."
            )

        if carrier_code is not None:
            track_info_id = package.get("FTrackInfoId")
            if not track_info_id:
                raise ToolError(
                    f"Tracking number {tracking_number} was found but has no "
                    "internal ID, so the carrier cannot be overridden."
                )
            await _17track.account_set_track_carrier(
                session,
                track_info_id,
                carrier_code,
                package.get("FSecondCarrier") or 0,
            )
            # Overriding a carrier makes 17track clear the package's cached
            # state and re-parse it for 30-60 minutes, so the shipment
            # fields are nulled and an explanatory note is returned (see
            # apply_carrier_override for the rationale).
            return _17track.apply_carrier_override(
                _17track.format_account_package(package, carriers),
                tracking_number,
                carrier_code,
                carriers,
            )

        # A package added moments ago has a carrier but no state or events
        # yet, which must not be reported as a package without events.
        return _17track.apply_pending_state(
            _17track.format_account_package(package, carriers)
        )


def main() -> None:
    params = json.load(sys.stdin)

    known_params = {"tracking_number", "carrier"}
    unknown = set(params.keys()) - known_params
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    tracking_number = params.get("tracking_number")
    if not tracking_number:
        raise ToolError("Missing required parameter: tracking_number")

    config = _17track.load_config()
    mode = _17track.resolve_auth_mode(config)
    carriers = _17track.load_carriers()

    # A carrier that is present but empty/whitespace must fail loudly: a
    # supplied carrier is never silently ignored.
    carrier_code = None
    if "carrier" in params:
        carrier_code = _17track.resolve_carrier(params["carrier"], carriers)

    if mode == "api":
        api_token = str(config["api_token"]).strip()
        deadline = _17track.api_deadline()
        registration = register_tracking_number(
            tracking_number, api_token, carrier_code, deadline
        )
        if registration["corrected_carrier"] is not None:
            # 17track corrected the requested carrier (documented /register
            # behaviour). The registration succeeded, so report the carrier
            # actually assigned plus an explanatory note instead of
            # discarding the success; do not fight the correction with
            # /changecarrier.
            raw_info = get_tracking_info(tracking_number, api_token, deadline)
            result = _17track.apply_carrier_correction(
                _17track.format_api_result(raw_info, carriers),
                carrier_code,
                registration["corrected_carrier"],
                carriers,
            )
        elif carrier_code is not None and not registration["registered"]:
            # The number was already tracked, so /register's carrier field
            # is a silent no-op. Apply the override via /changecarrier; a
            # carrier change makes 17track re-parse the package, so the
            # override response is returned instead of the stale data.
            change_carrier(tracking_number, api_token, carrier_code, deadline)
            result = _17track.apply_carrier_override(
                {}, tracking_number, carrier_code, carriers
            )
        else:
            raw_info = get_tracking_info(tracking_number, api_token, deadline)
            # Same pending window as account mode: /register succeeds long
            # before 17track has fetched anything from the carrier.
            result = _17track.apply_pending_state(
                _17track.format_api_result(raw_info, carriers)
            )
    else:
        email = str(config["email"]).strip()
        # A password may legitimately begin or end with whitespace, so it is
        # passed to the login call unmodified. resolve_auth_mode already
        # stripped a copy of the value only to decide whether it is empty.
        password = str(config["password"])
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    track_account_mode(
                        tracking_number, carrier_code, carriers, email, password
                    ),
                    timeout=_17track.ACCOUNT_PATH_DEADLINE_SECONDS,
                )
            )
        except asyncio.TimeoutError as err:
            raise ToolError(
                "The 17track account lookup exceeded the "
                f"{_17track.ACCOUNT_PATH_DEADLINE_SECONDS}-second tool budget "
                "and was aborted. Try again shortly."
            ) from err

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
