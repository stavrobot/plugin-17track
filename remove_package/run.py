#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "aiohttp", "pyseventeentrack"]
# ///

import json
import pathlib
import sys

import requests

# The shared root module holds config/auth resolution, the status map and
# carrier lookup. It lives at the plugin root so that every tool resolves
# configuration and auth mode the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _17track
from _17track import ToolError

DELETE_URL = "https://api.17track.net/track/v2.4/deletetrack"


def delete_tracking_number(
    tracking_number: str, api_token: str, timeout: float
) -> None:
    response = requests.post(
        DELETE_URL,
        headers={"17token": api_token},
        json=[{"number": tracking_number}],
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()

    rejected = body.get("data", {}).get("rejected", [])
    for rejection in rejected:
        if rejection.get("number") == tracking_number:
            message = rejection.get("error", {}).get("message", "Unknown error.")
            raise ToolError(f"Failed to remove tracking number: {message}")


def main() -> None:
    params = json.load(sys.stdin)

    known_params = {"tracking_number"}
    unknown = set(params.keys()) - known_params
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    tracking_number = params.get("tracking_number")
    if not tracking_number:
        raise ToolError("Missing required parameter: tracking_number")

    config = _17track.load_config()
    mode = _17track.resolve_auth_mode(config)
    if mode != "api":
        # A no-op would be worse than a refusal: the package would stay
        # tracked while the tool reports success.
        raise ToolError(
            "remove_package requires api_token authentication and is not "
            "available when the plugin is configured with email and password "
            "(account mode)."
        )

    api_token = str(config["api_token"]).strip()
    deadline = _17track.api_deadline()
    delete_tracking_number(
        tracking_number, api_token, _17track.api_timeout_remaining(deadline)
    )

    json.dump({"tracking_number": tracking_number, "removed": True}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
