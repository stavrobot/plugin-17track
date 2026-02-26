#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

import json
import sys
import pathlib
import time

import requests

BASE_URL = "https://api.17track.net/track/v1"

# Mapping from the 17track API's numeric status codes (field `track.e`) to
# human-readable strings. Codes not in this map are returned as-is so that
# new statuses from the API don't silently disappear.
STATUS_CODES: dict[int, str] = {
    0: "Not Found",
    10: "In Transit",
    20: "Expired",
    30: "Pick Up",
    35: "Undelivered",
    40: "Delivered",
    50: "Alert",
}


def read_config() -> dict:
    config_path = pathlib.Path(__file__).parent / ".." / "config.json"
    with config_path.open() as config_file:
        return json.load(config_file)


def register_tracking_number(tracking_number: str, api_token: str) -> None:
    response = requests.post(
        f"{BASE_URL}/register",
        headers={"17token": api_token},
        json=[{"number": tracking_number}],
    )
    response.raise_for_status()
    body = response.json()

    rejected = body.get("data", {}).get("rejected", [])
    for rejection in rejected:
        message = rejection.get("error", {}).get("message", "")
        # The API rejects already-registered numbers with a message containing
        # "registered". This is expected behaviour, not an error.
        if "registered" not in message.lower():
            print(
                f"Tracking number rejected: {message}",
                file=sys.stderr,
            )
            sys.exit(1)


def get_tracking_info(tracking_number: str, api_token: str) -> dict:
    # After registration, the API may not have indexed the number yet.
    # Retry once after a short delay if the first call returns nothing.
    for attempt in range(2):
        response = requests.post(
            f"{BASE_URL}/gettrackinfo",
            headers={"17token": api_token},
            json=[{"number": tracking_number}],
        )
        response.raise_for_status()
        body = response.json()

        accepted = body.get("data", {}).get("accepted", [])
        if accepted:
            return accepted[0]

        if attempt == 0:
            time.sleep(2)

    print("No tracking info returned for the given number.", file=sys.stderr)
    sys.exit(1)


def format_tracking_result(raw: dict) -> dict:
    track = raw.get("track") or {}
    latest_event = track.get("z0") or {}

    raw_status = track.get("e")
    status = STATUS_CODES.get(raw_status, raw_status)

    return {
        "tracking_number": raw.get("number"),
        "status": status,
        "carrier": track.get("w1"),
        "origin_country": track.get("b"),
        "destination_country": track.get("c"),
        "location": latest_event.get("c") or latest_event.get("d"),
        "latest_event": latest_event.get("z"),
        "timestamp": latest_event.get("a"),
    }


def main() -> None:
    params = json.load(sys.stdin)

    known_params = {"tracking_number"}
    unknown = set(params.keys()) - known_params
    if unknown:
        print(f"Unknown parameters: {', '.join(sorted(unknown))}", file=sys.stderr)
        sys.exit(1)

    tracking_number = params.get("tracking_number")
    if not tracking_number:
        print("Missing required parameter: tracking_number", file=sys.stderr)
        sys.exit(1)

    config = read_config()
    api_token = config["api_token"]

    register_tracking_number(tracking_number, api_token)
    raw_info = get_tracking_info(tracking_number, api_token)
    result = format_tracking_result(raw_info)

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
