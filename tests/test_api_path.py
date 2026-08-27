#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "aiohttp", "pyseventeentrack"]
# ///
"""Mocked checks for the API-path branching in track_package.

The REST API path cannot be exercised against the live API (the user's
token is dead and the API became a paid product), so the branching logic
below is the only verification it has. Every requests.post is replaced
with a fake: no network calls are made and no credentials are read.

Run with: uv run tests/test_api_path.py
"""
import asyncio
import importlib.util
import io
import json
import pathlib
import sys
import time
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _17track
from _17track import ToolError

spec = importlib.util.spec_from_file_location(
    "track_run", ROOT / "track_package" / "run.py"
)
track_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(track_run)

CARRIERS = json.loads((ROOT / "carriers.json").read_text())
YANWEN = 190012  # "YANWEN" in the vendored table.

FAILURES = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name} {detail}")
        FAILURES.append(name)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def make_post(responses):
    """Return (fake requests.post, recorded calls); pops one body per call."""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return FakeResponse(responses.pop(0))

    return post, calls


def register_rejected(number, code=-18019901, message=None, error=None):
    """A /register response rejecting the number with the given error."""
    if error is None:
        error = {
            "code": code,
            "message": message
            or f"Tracking number {number} is already registered.",
        }
    return {
        "code": 0,
        "data": {
            "accepted": [],
            "rejected": [{"number": number, "error": error}],
        },
    }


def register_accepted(number, carrier):
    return {
        "code": 0,
        "data": {
            "accepted": [{"number": number, "carrier": carrier}],
            "rejected": [],
        },
    }


def info_accepted(number, w1=3011):
    return {
        "code": 0,
        "data": {
            "accepted": [
                {
                    "number": number,
                    "track": {
                        "e": 40,
                        "w1": w1,
                        "b": 1,
                        "c": 2,
                        "z0": {
                            "a": "2026-01-01 00:00",
                            "c": "NYC",
                            "z": "Delivered",
                        },
                    },
                }
            ],
            "rejected": [],
        },
    }


def info_empty():
    return {"code": 0, "data": {"accepted": [], "rejected": []}}


def change_accepted(number, carrier):
    return {
        "code": 0,
        "data": {
            "accepted": [{"number": number, "carrier": carrier, "final_carrier": 0}],
            "rejected": [],
        },
    }


def run_main(params, responses):
    """Drive track_run.main() with a mocked API; return (stdout, calls)."""
    post, calls = make_post(responses)
    track_run.requests.post = post
    _17track.load_config = lambda: {"api_token": "tok"}
    _17track.load_carriers = lambda: CARRIERS
    buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(params))
        with redirect_stdout(buf):
            track_run.main()
    finally:
        sys.stdin = old_stdin
    return buf.getvalue(), calls


def run_main_expect_error(params, responses):
    """Like run_main, but returns the ToolError main() raised (or None)."""
    try:
        run_main(params, responses)
    except ToolError as err:
        return err
    return None


orig_load_config = _17track.load_config
orig_load_carriers = _17track.load_carriers

# ---------------------------------------------------------------------------
# 1. Duplicate registration without a carrier: exact -18019901 detection.
# ---------------------------------------------------------------------------
post, calls = make_post([register_rejected("ABC", -18019901)])
track_run.requests.post = post
newly = track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
check("1a: -18019901 rejection returns registered=False", newly == {"registered": False, "corrected_carrier": None}, detail=repr(newly))
check(
    "1b: no carrier key sent when none supplied",
    calls[0]["json"] == [{"number": "ABC"}],
    detail=repr(calls[0]["json"]),
)

# Full main() flow: duplicate, no carrier -> plain info fetch, no override.
out, calls = run_main(
    {"tracking_number": "ABC"},
    [register_rejected("ABC", -18019901), info_accepted("ABC")],
)
parsed = json.loads(out)
check("1c: duplicate without carrier still fetches info", parsed["status"] == "Delivered")
check(
    "1d: no /changecarrier call without a carrier",
    len(calls) == 2 and not any("changecarrier" in c["url"] for c in calls),
)

# ---------------------------------------------------------------------------
# 2. Rejections that are NOT duplicates fail loudly (code, not message).
# ---------------------------------------------------------------------------
post, calls = make_post(
    [
        register_rejected(
            "ABC",
            -18019911,
            message=(
                "The tracking number of this carrier can not be registered "
                "at the moment."
            ),
        )
    ]
)
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("2a: -18019911 is not treated as a duplicate", False)
except ToolError as err:
    check("2a: -18019911 is not treated as a duplicate", "rejected" in str(err), detail=str(err))

# No error code at all, but the message contains "registered": must fail
# loudly, not be misread as a duplicate.
post, calls = make_post(
    [register_rejected("ABC", error={"message": "Number already registered."})]
)
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("2b: missing error code fails loudly despite 'registered' message", False)
except ToolError as err:
    check("2b: missing error code fails loudly despite 'registered' message", "rejected" in str(err), detail=str(err))

post, calls = make_post(
    [
        register_rejected(
            "ABC", -18010012, message="The format of 'ABC' is invalid."
        )
    ]
)
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("2c: other rejection codes fail loudly", False)
except ToolError as err:
    check("2c: other rejection codes fail loudly", "format of 'ABC' is invalid" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
# 3. Registration success requires a matching accepted entry.
# ---------------------------------------------------------------------------
post, calls = make_post([register_accepted("ABC", YANWEN)])
track_run.requests.post = post
newly = track_run.register_tracking_number("ABC", "tok", YANWEN, _17track.api_deadline())
check("3a: accepted entry matching number+carrier is success", newly == {"registered": True, "corrected_carrier": None}, detail=repr(newly))
check(
    "3b: carrier code sent in the payload when supplied",
    calls[0]["json"] == [{"number": "ABC", "carrier": YANWEN}],
    detail=repr(calls[0]["json"]),
)

post, calls = make_post([register_accepted("ABC", YANWEN)])
track_run.requests.post = post
newly = track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
check("3c: accepted entry matching number is success without carrier", newly == {"registered": True, "corrected_carrier": None}, detail=repr(newly))

post, calls = make_post([register_accepted("OTHER", YANWEN)])
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", YANWEN, _17track.api_deadline())
    check("3d: accepted entry for a different number fails", False)
except ToolError as err:
    check("3d: accepted entry for a different number fails", "did not confirm" in str(err), detail=str(err))

post, calls = make_post([register_accepted("ABC", 3011)])
track_run.requests.post = post
newly = track_run.register_tracking_number("ABC", "tok", YANWEN, _17track.api_deadline())
check(
    "3e: accepted entry with a different carrier is a documented correction",
    newly == {"registered": True, "corrected_carrier": 3011},
    detail=repr(newly),
)

# An accepted entry that omits the carrier cannot confirm the requested
# carrier: the registration's carrier is unknown, so it must fail loudly.
post, calls = make_post([{"code": 0, "data": {"accepted": [{"number": "ABC"}], "rejected": []}}])
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", YANWEN, _17track.api_deadline())
    check("3e2: accepted entry without a carrier fails loudly", False)
except ToolError as err:
    check("3e2: accepted entry without a carrier fails loudly", "did not confirm" in str(err), detail=str(err))

post, calls = make_post([{"code": 0, "data": {"accepted": [], "rejected": []}}])
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("3f: empty accepted+rejected fails loudly", False)
except ToolError as err:
    check("3f: empty accepted+rejected fails loudly", "did not confirm" in str(err), detail=str(err))

post, calls = make_post([{"code": 0}])
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("3g: response without data fails loudly", False)
except ToolError as err:
    check("3g: response without data fails loudly", "unexpected response" in str(err), detail=str(err))

post, calls = make_post(
    [
        {
            "code": 0,
            "data": {"errors": [{"code": -18010013, "message": "Submitted data is invalid."}]},
        }
    ]
)
track_run.requests.post = post
try:
    track_run.register_tracking_number("ABC", "tok", None, _17track.api_deadline())
    check("3h: errors-shaped response fails loudly", False)
except ToolError as err:
    check("3h: errors-shaped response fails loudly", "did not confirm" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
# 4. /changecarrier success requires an accepted entry matching number AND
#    the requested carrier.
# ---------------------------------------------------------------------------
post, calls = make_post([change_accepted("ABC", YANWEN)])
track_run.requests.post = post
track_run.change_carrier("ABC", "tok", YANWEN, _17track.api_deadline())
check(
    "4a: matching accepted entry confirms the change (no error)",
    True,
)
check(
    "4b: changecarrier URL is v2.4",
    calls[0]["url"] == "https://api.17track.net/track/v2.4/changecarrier",
)
check(
    "4c: body has number+carrier_new, no carrier_old",
    calls[0]["json"] == [{"number": "ABC", "carrier_new": YANWEN}],
    detail=repr(calls[0]["json"]),
)
check("4d: 17token header present", calls[0]["headers"]["17token"] == "tok")

for label, body in (
    ("4e: empty accepted+rejected", {"code": 0, "data": {"accepted": [], "rejected": []}}),
    ("4f: empty accepted object", {"code": 0, "data": {"accepted": [{}], "rejected": []}}),
    ("4g: accepted entry for a different number", change_accepted("OTHER", YANWEN)),
    ("4h: accepted entry with a different carrier", change_accepted("ABC", 190013)),
):
    post, calls = make_post([body])
    track_run.requests.post = post
    try:
        track_run.change_carrier("ABC", "tok", YANWEN, _17track.api_deadline())
        check(f"{label} fails loudly", False)
    except ToolError as err:
        check(f"{label} fails loudly", "did not confirm" in str(err), detail=str(err))

post, calls = make_post(
    [
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [
                    {
                        "number": "ABC",
                        "carrier": 0,
                        "error": {
                            "code": -18019807,
                            "message": "The times for changing carrier exceed limit.",
                        },
                    }
                ],
            },
        }
    ]
)
track_run.requests.post = post
try:
    track_run.change_carrier("ABC", "tok", YANWEN, _17track.api_deadline())
    check("4i: refused change fails with the API message", False)
except ToolError as err:
    check(
        "4i: refused change fails with the API message",
        "refused the carrier change" in str(err) and "exceed limit" in str(err),
        detail=str(err),
    )

# ---------------------------------------------------------------------------
# 5. main() flows: duplicate with a carrier across confirmed, refused and
#    ambiguous /changecarrier responses; fresh registration skips it.
# ---------------------------------------------------------------------------
out, calls = run_main(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [register_rejected("ABC", -18019901), change_accepted("ABC", YANWEN)],
)
parsed = json.loads(out)
check("5a: confirmed change returns the override shape", parsed["carrier"] == "YANWEN" and parsed["status"] is None)
check("5b: confirmed change skips gettrackinfo", len(calls) == 2 and "changecarrier" in calls[1]["url"])
check("5c: carrier_new resolved from the name", calls[1]["json"][0]["carrier_new"] == YANWEN)

err = run_main_expect_error(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [
        register_rejected("ABC", -18019901),
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [
                    {
                        "number": "ABC",
                        "error": {
                            "code": -18019807,
                            "message": "The times for changing carrier exceed limit.",
                        },
                    }
                ],
            },
        },
    ],
)
check("5d: refused change propagates as an error", err is not None and "refused the carrier change" in str(err), detail=repr(err))

err = run_main_expect_error(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [register_rejected("ABC", -18019901), change_accepted("ABC", 190013)],
)
check("5e: ambiguous change (wrong carrier) propagates as an error", err is not None and "did not confirm" in str(err), detail=repr(err))

out, calls = run_main(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [register_accepted("ABC", YANWEN), info_accepted("ABC")],
)
parsed = json.loads(out)
check("5f: fresh registration skips /changecarrier", parsed["status"] == "Delivered")
check(
    "5g: fresh registration calls register then gettrackinfo only",
    len(calls) == 2 and not any("changecarrier" in c["url"] for c in calls),
)

# A corrected registration: /register returns a different carrier than the
# one requested. This is a documented success, so main() must report the
# assigned carrier (resolved to a name) plus a note, and must not call
# /changecarrier. The info response deliberately reports the REQUESTED
# carrier so the check proves the correction overwrites it.
out, calls = run_main(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [register_accepted("ABC", 3011), info_accepted("ABC", w1=YANWEN)],
)
parsed = json.loads(out)
check("5h: corrected registration still succeeds", parsed["status"] == "Delivered")
check("5i: corrected registration reports the assigned carrier", parsed["carrier"] == "China Post", detail=repr(parsed.get("carrier")))
check(
    "5j: correction note names both carriers",
    "YANWEN" in parsed["note"] and "China Post" in parsed["note"] and "not accepted" in parsed["note"],
    detail=repr(parsed.get("note")),
)
check(
    "5k: corrected registration skips /changecarrier",
    len(calls) == 2 and not any("changecarrier" in c["url"] for c in calls),
)

# ---------------------------------------------------------------------------
# 6. Time budget: expired deadline issues no request; the retry sleep is
#    deadline-aware.
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


orig_monotonic = time.monotonic
orig_sleep = time.sleep
try:
    clock = FakeClock()
    time.monotonic = clock.monotonic
    time.sleep = clock.sleep

    # Expired deadline: register raises before any request is attempted.
    post, calls = make_post([])
    track_run.requests.post = post
    try:
        track_run.register_tracking_number(
            "ABC", "tok", None, clock.monotonic() - 1.0
        )
        check("6a: expired deadline raises", False)
    except ToolError as err:
        check("6a: expired deadline raises", "budget" in str(err), detail=str(err))
    check("6b: expired deadline issues no request", len(calls) == 0)

    # Expired deadline on the info retry path as well.
    post, calls = make_post([])
    track_run.requests.post = post
    try:
        track_run.get_tracking_info("ABC", "tok", clock.monotonic() - 1.0)
        check("6c: expired deadline aborts the info fetch", False)
    except ToolError as err:
        check("6c: expired deadline aborts the info fetch", "budget" in str(err), detail=str(err))
    check("6d: expired deadline issues no info request", len(calls) == 0)

    # Budget comfortably covers the sleep: sleep happens, second attempt
    # runs with the remaining budget.
    post, calls = make_post([info_empty(), info_accepted("ABC")])
    track_run.requests.post = post
    result = track_run.get_tracking_info(
        "ABC", "tok", clock.monotonic() + 25.0
    )
    check("6e: retry sleep consumed when the budget covers it", clock.sleeps == [2.0], detail=repr(clock.sleeps))
    check("6f: info returned after the retry", result["number"] == "ABC")
    check(
        "6g: request timeouts derived from the remaining budget",
        abs(calls[0]["timeout"] - 25.0) < 1e-9 and abs(calls[1]["timeout"] - 23.0) < 1e-9,
        detail=repr([c["timeout"] for c in calls]),
    )

    # Little budget left (1.5s < 2s sleep): the sleep is skipped and the
    # second attempt still runs inside the deadline.
    sleeps_before = len(clock.sleeps)
    post, calls = make_post([info_empty(), info_accepted("ABC")])
    track_run.requests.post = post
    result = track_run.get_tracking_info(
        "ABC", "tok", clock.monotonic() + 1.5
    )
    check("6h: retry sleep skipped when little budget remains", len(clock.sleeps) == sleeps_before, detail=repr(clock.sleeps))
    check("6i: second attempt still issued", len(calls) == 2)
    check(
        "6j: second attempt timeout bounded by the remaining budget",
        abs(calls[1]["timeout"] - 1.5) < 1e-9,
        detail=repr([c["timeout"] for c in calls]),
    )
finally:
    time.monotonic = orig_monotonic
    time.sleep = orig_sleep

# ---------------------------------------------------------------------------
# 7. Whitespace-preserving password forwarding.
# ---------------------------------------------------------------------------
captured = {}


async def fake_track_account_mode(tracking_number, carrier_code, carriers, email, password):
    captured["password"] = password
    captured["email"] = email
    return {"tracking_number": tracking_number}


orig_account_mode = track_run.track_account_mode
track_run.track_account_mode = fake_track_account_mode
_17track.load_config = lambda: {
    "email": "user@example.com",
    "password": "  secret with spaces  ",
}
_17track.load_carriers = lambda: CARRIERS
buf = io.StringIO()
old_stdin = sys.stdin
try:
    sys.stdin = io.StringIO(json.dumps({"tracking_number": "ABC"}))
    with redirect_stdout(buf):
        track_run.main()
finally:
    sys.stdin = old_stdin
track_run.track_account_mode = orig_account_mode
check(
    "7a: password reaches the login call unmodified",
    captured.get("password") == "  secret with spaces  ",
    detail=repr(captured.get("password")),
)
check("7b: email is still stripped", captured.get("email") == "user@example.com")

try:
    _17track.resolve_auth_mode({"email": "user@example.com", "password": "   "})
    check("7c: whitespace-only password is still treated as empty", False)
except ToolError:
    check("7c: whitespace-only password is still treated as empty", True)

# ---------------------------------------------------------------------------
# 8. Carrier name resolution rules.
# ---------------------------------------------------------------------------
check("8a: exact case-insensitive name match", _17track.resolve_carrier("yanwen", CARRIERS) == YANWEN)
check("8b: unique substring match", _17track.resolve_carrier("Yanw", CARRIERS) == YANWEN)

try:
    _17track.resolve_carrier("GLS (", CARRIERS)
    check("8c: ambiguous name lists candidates", False)
except ToolError as err:
    check("8c: ambiguous name lists candidates", "Candidates:" in str(err), detail=str(err))

check("8d: all-digit carrier code passes through", _17track.resolve_carrier("190012", CARRIERS) == YANWEN)

try:
    _17track.resolve_carrier("Definitely Not A Carrier", CARRIERS)
    check("8e: unknown name fails", False)
except ToolError as err:
    check("8e: unknown name fails", "No carrier matches" in str(err), detail=str(err))

try:
    _17track.resolve_carrier("   ", CARRIERS)
    check("8f: empty carrier fails", False)
except ToolError as err:
    check("8f: empty carrier fails", "empty" in str(err), detail=str(err))

try:
    _17track.resolve_carrier("Yanwen", None)
    check("8g: missing table fails", False)
except ToolError as err:
    check("8g: missing table fails", "missing or unreadable" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
# 9. Pending packages: a not-yet-fetched package must never report a null
#    status, because that reads as "this package has no events".
#    The live pending window could not be reproduced (it lasts minutes and
#    needs a freshly added number), so these are the only checks it has.
# ---------------------------------------------------------------------------
UPS = 100002  # "UPS" in the vendored table.


def account_package(state, last_event, carrier=UPS, remark=None):
    """A raw GetTrackInfoList package dict, as account mode receives it."""
    package = {"FTrackNo": "ABC", "FFirstCarrier": carrier}
    if state is not None:
        package["FPackageState"] = state
    if last_event is not None:
        package["FLastEvent"] = json.dumps(last_event)
    if remark is not None:
        package["FRemark"] = remark
    return package


# Account mode: carrier assigned, nothing fetched yet.
pending = _17track.apply_pending_state(
    _17track.format_account_package(account_package(None, None), CARRIERS)
)
check("9a: absent state and no event yields pending status", pending["status"] == _17track.PENDING_STATUS, detail=repr(pending))
check("9b: pending keeps the detected carrier", pending["carrier"] == "UPS", detail=repr(pending))
check(
    "9c: pending omits the unknown fields rather than nulling them",
    not {"location", "latest_event", "timestamp", "origin_country", "destination_country"} & set(pending),
    detail=repr(sorted(pending)),
)
check("9d: pending explains itself", "few minutes" in pending["note"], detail=repr(pending.get("note")))

# "Not Found" is a real answer from 17track, not a pending state.
not_found = _17track.apply_pending_state(
    _17track.format_account_package(account_package(0, None), CARRIERS)
)
check("9e: FPackageState 0 stays Not Found", not_found["status"] == "Not Found", detail=repr(not_found))
check("9f: Not Found is not rewritten as pending", "note" not in not_found, detail=repr(not_found))

# A fully fetched package must pass through untouched.
fetched_raw = _17track.format_account_package(
    account_package(10, {"a": "2026-01-01 00:00", "c": "NYC", "z": "In transit"}),
    CARRIERS,
)
fetched = _17track.apply_pending_state(dict(fetched_raw))
check("9g: fetched package is untouched", fetched == fetched_raw, detail=repr(fetched))

# A friendly name survives the rewrite; it is user data, not carrier data.
named = _17track.apply_pending_state(
    _17track.format_account_package(account_package(None, None, remark="Huel"), CARRIERS)
)
check("9h: pending preserves friendly_name", named.get("friendly_name") == "Huel", detail=repr(named))

# API mode: an accepted entry whose track is empty is the same pending state.
out, calls = run_main(
    {"tracking_number": "ABC"},
    [
        register_accepted("ABC", UPS),
        {"code": 0, "data": {"accepted": [{"number": "ABC", "track": {}}], "rejected": []}},
    ],
)
parsed = json.loads(out)
check("9i: API mode empty track yields pending", parsed["status"] == _17track.PENDING_STATUS, detail=out)
check("9j: API mode pending omits null event fields", "latest_event" not in parsed, detail=out)

# The carrier-override response is deliberately all-null with its own note.
# apply_pending_state must not be reached on that path and rewrite it.
out, calls = run_main(
    {"tracking_number": "ABC", "carrier": "Yanwen"},
    [register_rejected("ABC", -18019901), change_accepted("ABC", YANWEN)],
)
parsed = json.loads(out)
check("9k: carrier override keeps its own note", parsed["status"] is None and "was set to" in parsed["note"], detail=out)

# ---------------------------------------------------------------------------
# 10. AddTrackNo duplicate detection by code, not just by message.
# ---------------------------------------------------------------------------
check(
    "10a: duplicate add code is defined",
    _17track.DUPLICATE_ADD_CODE == -11010101,
    detail=repr(_17track.DUPLICATE_ADD_CODE),
)


def add_track_no_result(response):
    """Run account_add_tracking_number against a canned buyer response."""
    orig = _17track.account_buyer_call

    async def fake(session, method, param):
        return response

    _17track.account_buyer_call = fake
    try:
        asyncio.run(_17track.account_add_tracking_number(None, "ABC"))
        return None
    except ToolError as err:
        return err
    finally:
        _17track.account_buyer_call = orig


check("10b: code 0 accepted", add_track_no_result({"Code": 0}) is None)
check(
    "10c: -11010101 accepted with a non-English message",
    add_track_no_result({"Code": -11010101, "Message": "Numero gia yparxei"}) is None,
)
check(
    "10d: 'exists' message still accepted as a fallback",
    add_track_no_result({"Code": -1, "Message": "Tracking number exists."}) is None,
)
check(
    "10e: an unrelated failure still fails loudly",
    isinstance(add_track_no_result({"Code": -999, "Message": "Quota exceeded."}), ToolError),
)

# ---------------------------------------------------------------------------
_17track.load_config = orig_load_config
_17track.load_carriers = orig_load_carriers

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
