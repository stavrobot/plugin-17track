# plugin-17track

A Stavrobot plugin for tracking packages via the [17track.net](https://www.17track.net/) API.

## Tools

- **track_package** — register a tracking number and return its current status.
- **list_packages** — list all tracked packages and their statuses.
- **remove_package** — remove a tracking number from the account.

## Installation

Install the plugin by asking the bot to install https://github.com/stavrobot/plugin-17track.git.

## Configuration

This plugin requires a 17track API token. To obtain one:

1. Register at https://features.17track.net/en/api (each account gets 100 free tracking quota).
2. Log in and go to Settings -> Security -> Access Key.
3. Set the `api_token` configuration value to your access key.

## Regenerating the carrier table

`carriers.json` (carrier code -> name) is vendored. Regenerate it by hand with:

```sh
curl -s https://res.17track.net/asset/carrier/info/apicarrier.all.json | python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps({str(e['key']):e['_name'] for e in d},ensure_ascii=False,separators=(',',':')))" > carriers.json
```
