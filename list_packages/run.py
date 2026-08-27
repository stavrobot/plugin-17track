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

BASE_URL = "https://api.17track.net/track/v1"


def get_tracked_numbers(api_token: str, timeout: float) -> list[str]:
    response = requests.post(
        f"{BASE_URL}/gettracklist",
        headers={"17token": api_token},
        json={},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()

    accepted = body.get("data", {}).get("accepted", [])
    return [item["number"] for item in accepted]


def get_tracking_info_batch(
    numbers: list[str], api_token: str, deadline: float
) -> list[dict]:
    # The API accepts at most 40 numbers per request.
    batch_size = 40
    results = []
    for offset in range(0, len(numbers), batch_size):
        batch = numbers[offset : offset + batch_size]
        response = requests.post(
            f"{BASE_URL}/gettrackinfo",
            headers={"17token": api_token},
            json=[{"number": number} for number in batch],
            timeout=_17track.api_timeout_remaining(deadline),
        )
        response.raise_for_status()
        body = response.json()
        accepted = body.get("data", {}).get("accepted", [])
        results.extend(accepted)
    return results


def main() -> None:
    params = json.load(sys.stdin)

    if params:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(params.keys()))}")

    config = _17track.load_config()
    mode = _17track.resolve_auth_mode(config)
    if mode != "api":
        # A no-op would be worse than a refusal: an empty list would make
        # the assistant confidently report that nothing is tracked.
        raise ToolError(
            "list_packages requires api_token authentication and is not "
            "available when the plugin is configured with email and password "
            "(account mode)."
        )

    carriers = _17track.load_carriers()
    api_token = str(config["api_token"]).strip()

    # Bound the whole API path: gettracklist plus one gettrackinfo per
    # batch of 40 numbers, each request timed out from the remaining budget.
    deadline = _17track.api_deadline()
    numbers = get_tracked_numbers(
        api_token, _17track.api_timeout_remaining(deadline)
    )
    raw_results = get_tracking_info_batch(numbers, api_token, deadline)
    packages = [_17track.format_api_result(raw, carriers) for raw in raw_results]

    json.dump({"packages": packages}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
