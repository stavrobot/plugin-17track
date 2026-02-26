#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

import json
import sys
import pathlib

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


def get_tracked_numbers(api_token: str) -> list[str]:
    response = requests.post(
        f"{BASE_URL}/gettracklist",
        headers={"17token": api_token},
        json={},
    )
    response.raise_for_status()
    body = response.json()

    accepted = body.get("data", {}).get("accepted", [])
    return [item["number"] for item in accepted]


def get_tracking_info_batch(numbers: list[str], api_token: str) -> list[dict]:
    # The API accepts at most 40 numbers per request.
    batch_size = 40
    results = []
    for offset in range(0, len(numbers), batch_size):
        batch = numbers[offset : offset + batch_size]
        response = requests.post(
            f"{BASE_URL}/gettrackinfo",
            headers={"17token": api_token},
            json=[{"number": number} for number in batch],
        )
        response.raise_for_status()
        body = response.json()
        accepted = body.get("data", {}).get("accepted", [])
        results.extend(accepted)
    return results


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

    if params:
        print(
            f"Unknown parameters: {', '.join(sorted(params.keys()))}",
            file=sys.stderr,
        )
        sys.exit(1)

    config = read_config()
    api_token = config["api_token"]

    numbers = get_tracked_numbers(api_token)
    raw_results = get_tracking_info_batch(numbers, api_token)
    packages = [format_tracking_result(raw) for raw in raw_results]

    json.dump({"packages": packages}, sys.stdout)


if __name__ == "__main__":
    main()
