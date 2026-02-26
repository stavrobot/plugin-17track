#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

import json
import sys
import pathlib

import requests

DELETE_URL = "https://api.17track.net/track/v2.4/deletetrack"


def read_config() -> dict:
    config_path = pathlib.Path(__file__).parent / ".." / "config.json"
    with config_path.open() as config_file:
        return json.load(config_file)


def delete_tracking_number(tracking_number: str, api_token: str) -> None:
    response = requests.post(
        DELETE_URL,
        headers={"17token": api_token},
        json=[{"number": tracking_number}],
    )
    response.raise_for_status()
    body = response.json()

    rejected = body.get("data", {}).get("rejected", [])
    for rejection in rejected:
        if rejection.get("number") == tracking_number:
            message = rejection.get("error", {}).get("message", "Unknown error.")
            print(f"Failed to remove tracking number: {message}", file=sys.stderr)
            sys.exit(1)


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

    delete_tracking_number(tracking_number, api_token)

    json.dump({"tracking_number": tracking_number, "removed": True}, sys.stdout)


if __name__ == "__main__":
    main()
