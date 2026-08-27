"""Shared internals for the 17track plugin tools.

Every tool needs the same handful of things regardless of auth mode: config
loading, auth-mode resolution, the status map, carrier code/name lookup
against the vendored table, and - in account mode - the raw buyer.17track.net
calls issued after a pyseventeentrack login (the library's Package class
discards the carrier field, so formatting is done from the raw JSON here).

Tools import this module by inserting the plugin root on sys.path, the same
way they already locate ../config.json:

    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    import _17track
"""

import json
import logging
import pathlib
import time

import aiohttp
from pyseventeentrack import Client
from pyseventeentrack.errors import RequestError
from pyseventeentrack.package import COUNTRY_MAP

ROOT = pathlib.Path(__file__).resolve().parent

BUYER_API_URL = "https://buyer.17track.net/orderapi/call"

# 17track status codes are numeric and identical across the REST and buyer
# API surfaces. Codes not in this map are returned as-is so that new
# statuses don't silently disappear.
STATUS_CODES: dict[int, str] = {
    0: "Not Found",
    10: "In Transit",
    20: "Expired",
    30: "Pick Up",
    35: "Undelivered",
    40: "Delivered",
    50: "Alert",
}

# Both modes must fit the runner's hard ~30s kill. Account mode: login +
# AddTrackNo + GetTrackInfoList (+ one retry) + optional SetTrackCarrier,
# bounded as a whole by asyncio.wait_for(ACCOUNT_PATH_DEADLINE_SECONDS).
# API mode: register + gettrackinfo (+ one retry) + optional changecarrier,
# and for list_packages a gettracklist plus one gettrackinfo per 40 numbers.
# requests timeouts are per-read, not wall clock, so a single request can
# consume its entire remaining budget. The API path is therefore bounded by
# deriving every request timeout from the time left under one overall
# deadline (API_PATH_DEADLINE_SECONDS, well below the kill) and by skipping
# the retry sleep once the budget can no longer cover it (lookup_retry_sleep).
# Worst case is then bounded by the deadline itself, not deadline + sleep.
REQUEST_TIMEOUT_SECONDS = 8
LOOKUP_RETRY_SLEEP_SECONDS = 2
ACCOUNT_PATH_DEADLINE_SECONDS = 25
API_PATH_DEADLINE_SECONDS = 20
LOOKUP_PAGE_SIZE = 40


class ToolError(Exception):
    """Fatal, user-facing error whose message is safe to print."""


def load_config() -> dict:
    try:
        with (ROOT / "config.json").open() as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as err:
        raise ToolError(f"Could not read config.json: {err}") from err
    if not isinstance(config, dict):
        raise ToolError("config.json must contain a JSON object.")
    return config


def resolve_auth_mode(config: dict) -> str:
    """Return 'api' or 'account' depending on which credentials are set."""
    if str(config.get("api_token") or "").strip():
        return "api"
    if (
        str(config.get("email") or "").strip()
        and str(config.get("password") or "").strip()
    ):
        return "account"
    raise ToolError(
        "No usable 17track credentials found in config.json. Set either "
        "api_token (official API mode) or email and password (account mode)."
    )


def load_carriers() -> dict | None:
    """Return the vendored carrier table, or None if it is missing/damaged.

    Callers degrade gracefully on None: numeric carrier codes are reported
    as-is instead of being mapped to names.
    """
    try:
        with (ROOT / "carriers.json").open() as carrier_file:
            carriers = json.load(carrier_file)
    except (OSError, ValueError):
        # ValueError covers both JSONDecodeError and UnicodeDecodeError, so
        # a wrongly encoded table degrades exactly like an unparseable one.
        return None
    return carriers if isinstance(carriers, dict) else None


def resolve_carrier(carrier_param: str, carriers: dict | None) -> int:
    """Resolve a caller-supplied carrier name (or code) to a numeric code.

    A confidently wrong carrier name from a caller must fail loudly rather
    than be silently ignored, so every failure mode raises ToolError.
    """
    query = str(carrier_param).strip()
    if not query:
        raise ToolError("The 'carrier' parameter is empty.")

    if carriers is None:
        raise ToolError(
            "carriers.json is missing or unreadable, so the carrier name "
            "cannot be resolved. Restore the file and retry."
        )

    lowered = query.lower()

    # 1. Exact case-insensitive name match wins.
    for code, name in carriers.items():
        if str(name).lower() == lowered:
            return int(code)

    # 2. Case-insensitive substring match: exactly one hit, use it.
    hits = [
        (code, name)
        for code, name in carriers.items()
        if lowered in str(name).lower()
    ]
    if len(hits) == 1:
        return int(hits[0][0])

    # 3. Several hits: fail loudly and list candidates so the caller can retry.
    if len(hits) > 1:
        candidates = sorted(name for _, name in hits)[:10]
        raise ToolError(
            f"The carrier name '{carrier_param}' is ambiguous. Candidates: "
            + ", ".join(candidates)
            + ". Retry with one of these exact names."
        )

    # 4. An all-digit string that is a valid carrier code is used directly.
    if query.isdigit() and query in carriers:
        return int(query)

    raise ToolError(
        f"No carrier matches '{carrier_param}'. Check the spelling against "
        "17track's carrier names."
    )


def carrier_code_to_name(code, carriers: dict | None):
    """Best-effort code -> name; degrades to the raw code.

    Numeric codes (int or digit string) are looked up in the table and
    degrade to the bare number when missing. Non-numeric strings are passed
    through unchanged, matching normalize_carrier, so a carrier value the
    API already returned as a name never crashes the lookup.
    """
    if code is None:
        return None
    if isinstance(code, str):
        if not code.isdigit():
            return code
        code = int(code)
    if carriers is None:
        return str(code)
    name = carriers.get(str(code))
    return name if name else str(code)


def normalize_carrier(value, carriers: dict | None):
    """Map numeric carrier codes to names; pass strings through unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return carrier_code_to_name(value, carriers)
    return value


def normalize_country(value):
    """Map numeric country codes to names; pass strings through unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        name = COUNTRY_MAP.get(value)
        return name if name is not None else str(value)
    return value


def normalize_status(value):
    return STATUS_CODES.get(value, value)


def format_api_result(raw: dict, carriers: dict | None) -> dict:
    """Format a REST API gettrackinfo entry (API mode)."""
    track = raw.get("track") or {}
    latest_event = track.get("z0") or {}

    return {
        "tracking_number": raw.get("number"),
        "status": normalize_status(track.get("e")),
        "carrier": normalize_carrier(track.get("w1"), carriers),
        "origin_country": normalize_country(track.get("b")),
        "destination_country": normalize_country(track.get("c")),
        "location": latest_event.get("c") or latest_event.get("d"),
        "latest_event": latest_event.get("z"),
        "timestamp": latest_event.get("a"),
    }


def format_account_package(package: dict, carriers: dict | None) -> dict:
    """Format a raw GetTrackInfoList package dict (account mode)."""
    last_event = {}
    raw_event = package.get("FLastEvent")
    if raw_event:
        try:
            parsed = json.loads(raw_event)
        except json.JSONDecodeError:
            parsed = {}
        last_event = parsed if isinstance(parsed, dict) else {}

    result = {
        "tracking_number": package.get("FTrackNo"),
        "status": normalize_status(package.get("FPackageState")),
        "carrier": normalize_carrier(package.get("FFirstCarrier"), carriers),
        "origin_country": normalize_country(package.get("FFirstCountry")),
        "destination_country": normalize_country(package.get("FSecondCountry")),
        "location": " ".join(
            part for part in (last_event.get("c"), last_event.get("d")) if part
        ).strip()
        or None,
        "latest_event": last_event.get("z"),
        "timestamp": last_event.get("a"),
    }

    friendly_name = package.get("FRemark")
    if friendly_name:
        result["friendly_name"] = friendly_name

    return result


def apply_carrier_override(
    result: dict, tracking_number, carrier_code, carriers: dict | None
) -> dict:
    """Turn a formatted package result into the carrier-override response.

    Changing a carrier makes 17track discard the old carrier's data and
    re-parse the package for up to an hour. Reporting the repudiated
    carrier's status next to the override note would give the caller
    contradictory signals, so every shipment field is nulled and an
    explanation is returned instead. Every key is kept so the response
    shape matches a normal one; any extra keys already present on the
    result (such as account-mode friendly_name) are preserved.
    """
    carrier_name = carrier_code_to_name(carrier_code, carriers)
    result["tracking_number"] = tracking_number
    result["carrier"] = carrier_name
    for key in (
        "status",
        "origin_country",
        "destination_country",
        "location",
        "latest_event",
        "timestamp",
    ):
        result[key] = None
    result["note"] = (
        f"The carrier for this package was set to {carrier_name}. "
        "17track is re-parsing the package; tracking details should appear "
        "within the hour."
    )
    return result


def apply_carrier_correction(
    result: dict, requested_code, assigned, carriers: dict | None
) -> dict:
    """Report a /register carrier correction on a formatted result.

    The /register docs state that when a supplied carrier code is invalid
    or belongs to another carrier, the system corrects it and returns the
    code it actually assigned. That is a documented success, not an error:
    the carrier field is set to the assigned carrier (resolved to a name)
    and a note records the correction, so the assistant can tell the user
    which carrier 17track used instead of the requested one.
    """
    requested_name = carrier_code_to_name(requested_code, carriers)
    assigned_name = carrier_code_to_name(assigned, carriers)
    result["carrier"] = assigned_name
    result["note"] = (
        f"The requested carrier ({requested_name}) was not accepted; "
        f"17track registered the package under {assigned_name} instead."
    )
    return result


def api_deadline() -> float:
    """Start the overall time budget for the synchronous REST (API) path."""
    return time.monotonic() + API_PATH_DEADLINE_SECONDS


def api_timeout_remaining(deadline: float) -> float:
    """Per-request timeout derived from the time left in the API budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError(
            "The 17track API calls exceeded the "
            f"{API_PATH_DEADLINE_SECONDS}-second tool budget and were "
            "aborted. Try again shortly."
        )
    return remaining


def lookup_retry_sleep(deadline) -> None:
    """Sleep before a lookup retry, but never past the overall API deadline.

    requests timeouts are per-read, not wall clock, so one request can
    consume nearly the whole budget. An unconditional sleep would overshoot
    the deadline (e.g. 25 + 2 = 27s against a 30s kill); instead the sleep
    is skipped when the remaining budget cannot cover it, and the next
    request then runs against whatever time is left (or is aborted by
    api_timeout_remaining).
    """
    if deadline is None:
        time.sleep(LOOKUP_RETRY_SLEEP_SECONDS)
        return
    if deadline - time.monotonic() > LOOKUP_RETRY_SLEEP_SECONDS:
        time.sleep(LOOKUP_RETRY_SLEEP_SECONDS)


def _silence_pyseventeentrack_logging() -> None:
    """Stop the library's loggers from leaking session data to stderr.

    pyseventeentrack logs raw request/response bodies (including the login
    response, which carries account and session data) at DEBUG and the full
    malformed login response at ERROR. The tool harness surfaces stderr to
    the assistant, so the library must never be allowed to print them.
    """
    for name in (
        "pyseventeentrack",
        "pyseventeentrack.client",
        "pyseventeentrack.profile",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)


async def account_login(
    session: aiohttp.ClientSession, email: str, password: str
) -> None:
    """Log in through pyseventeentrack on a caller-supplied session.

    The session must be created by the caller: Client._request otherwise
    opens a new aiohttp ClientSession per request and discards the login
    cookie, which makes every later call fail as not-logged-in.
    """
    _silence_pyseventeentrack_logging()
    client = Client(session=session)
    try:
        logged_in = await client.profile.login(email, password)
    except (RequestError, aiohttp.ClientError, json.JSONDecodeError) as err:
        raise ToolError(f"17track login request failed: {err}") from err
    if not logged_in:
        raise ToolError(
            "17track account login failed. Check the email and password in "
            "config.json."
        )


async def account_buyer_call(
    session: aiohttp.ClientSession, method: str, param: dict
) -> dict:
    """Issue a raw POST to buyer.17track.net and return the JSON body."""
    payload = {"version": "1.0", "method": method, "param": param, "sourcetype": 0}
    try:
        async with session.post(BUYER_API_URL, json=payload) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, json.JSONDecodeError) as err:
        raise ToolError(
            f"17track account API call '{method}' failed: {err}"
        ) from err
    if not isinstance(data, dict):
        raise ToolError(
            f"17track account API call '{method}' returned an unexpected response."
        )
    return data


async def account_add_tracking_number(
    session: aiohttp.ClientSession, tracking_number: str
) -> None:
    data = await account_buyer_call(
        session, "AddTrackNo", {"TrackNos": [tracking_number]}
    )
    code = data.get("Code")
    if code == 0:
        return
    # Re-adding a number already on the account is not an error: the tool is
    # idempotent, matching REST-mode behaviour for already-registered numbers.
    message = str(data.get("Message") or "")
    if "exists" in message.lower():
        return
    raise ToolError(
        f"17track could not add the tracking number ({message or f'code {code}'})."
    )


def _list_param(item: str = "") -> dict:
    return {
        "IsArchived": False,
        "Item": item,
        "Page": 1,
        "PerPage": LOOKUP_PAGE_SIZE,
        "PackageState": "",
        "Sequence": "0",
    }


async def account_find_package(
    session: aiohttp.ClientSession, tracking_number: str
) -> dict | None:
    """Return the raw package dict whose FTrackNo matches, or None.

    The 'Item' param filters server-side by tracking number on the live
    API, so a single page is enough and no page sweep is needed.
    """
    data = await account_buyer_call(
        session, "GetTrackInfoList", _list_param(item=tracking_number)
    )
    # A nonzero code means the buyer API refused the call (typically an
    # invalidated session), not that the package is missing.
    code = data.get("Code")
    if code != 0:
        raise ToolError(
            "17track refused the package list request "
            f"(code {code}). This usually means the account session is no "
            "longer valid; check the credentials in config.json and retry."
        )
    for package in data.get("Json") or []:
        if package.get("FTrackNo") == tracking_number:
            return package
    return None


async def account_set_track_carrier(
    session: aiohttp.ClientSession,
    track_info_id,
    first_carrier: int,
    second_carrier: int,
) -> None:
    """Override the carrier of an already-added package (numeric codes)."""
    data = await account_buyer_call(
        session,
        "SetTrackCarrier",
        {
            "TrackInfoId": track_info_id,
            "FirstCarrier": int(first_carrier),
            "SecondCarrier": int(second_carrier or 0),
        },
    )
    if data.get("Code") != 0:
        raise ToolError(
            "17track rejected the carrier override "
            f"({data.get('Message') or 'code ' + str(data.get('Code'))})."
        )
