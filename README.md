# plugin-17track

A Stavrobot plugin for tracking packages via [17track.net](https://www.17track.net/).

## Tools

- **track_package** — register a tracking number and return its current status. Works with either authentication mode.
- **list_packages** — list all tracked packages and their statuses. Carrier and country are reported as names (mapped from 17track's numeric codes via the vendored `carriers.json`), not raw codes; an unknown code is reported as the bare number. Requires `api_token` authentication.
- **remove_package** — remove a tracking number. Requires `api_token` authentication.

## Installation

Install the plugin by asking the bot to install https://github.com/stavrobot/plugin-17track.git.

## Configuration

The plugin supports two authentication modes. Pick ONE and set only those values. If both happen to be set, `api_token` takes precedence.

### 1. API token (official REST API)

Set `api_token` to your 17track REST API access key:

1. Register at https://features.17track.net/en/api and log in.
2. Copy your access key (Settings -> Security -> Access Key).

Note: the 17track REST API is now a paid product. A free trial is available, but for ongoing use most users are better served by the account option below. This is also the only mode supported by `list_packages` and `remove_package`.

### 2. 17track.net account (email + password)

Set `email` and `password` to your regular 17track.net account login:

1. Create an account at https://www.17track.net/.
2. Use the same email and password in the plugin config.

This mode needs no API registration and works with `track_package`. Warning: it uses the unofficial API that the 17track website itself uses. It is not a documented public API, is not supported by 17track, and can break without notice.

## Newly added tracking numbers

When you track a number for the first time, 17track detects the carrier straight away but takes a few minutes to fetch anything from that carrier. During that window `track_package` reports a status of `Pending`, along with the detected carrier and a note explaining that no events have arrived yet. This is not the same as the package having no tracking events — just ask again a few minutes later.

A status of `Not Found` is different: it means 17track did check with the carrier, and the carrier has nothing on record for that number.

## Carrier overrides

`track_package` accepts an optional `carrier` parameter. Supply it only when 17track detected the wrong carrier. The tool then overrides the carrier, and 17track re-parses the package for up to an hour. During that time tracking details are unavailable: the result reports the newly set carrier and a note explaining this, instead of fresh details.

## Tests

The REST API path cannot be exercised against the live API (the REST API is now a paid product), so its branching logic — duplicate-registration detection, `/changecarrier` response validation, and the time budget — is verified with mocked HTTP responses instead. The same applies to the `Pending` state, which lasts only minutes and cannot be reproduced on demand. The checks make no network calls and read no credentials. Run them with:

```sh
uv run tests/test_api_path.py
```

## Regenerating the carrier table

`carriers.json` (carrier code -> name) is vendored. Regenerate it by hand with:

```sh
curl -s https://res.17track.net/asset/carrier/info/apicarrier.all.json | python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps({str(e['key']):e['_name'] for e in d},ensure_ascii=False,separators=(',',':')))" > carriers.json
```
