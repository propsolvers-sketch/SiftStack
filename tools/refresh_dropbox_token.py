"""One-time helper to re-mint a Dropbox OAuth2 refresh token.

Run when the existing DROPBOX_REFRESH_TOKEN in .env returns
`AuthError('invalid_access_token')`. The mint is good for the life of the
Dropbox app — only re-run if the token gets revoked again.

Usage:
    .venv/bin/python tools/refresh_dropbox_token.py

Output: prints the new refresh token. Paste it into .env as
`DROPBOX_REFRESH_TOKEN=<new_value>` (replacing the old one).
"""

import sys
import webbrowser
from pathlib import Path

# Make src/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests
import config


def main() -> int:
    if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET:
        print("ERROR: DROPBOX_APP_KEY and DROPBOX_APP_SECRET must be set in .env first.")
        print("Get them from https://www.dropbox.com/developers/apps → your app → 'App key' and 'App secret'")
        return 1

    auth_url = (
        "https://www.dropbox.com/oauth2/authorize"
        f"?client_id={config.DROPBOX_APP_KEY}"
        "&response_type=code"
        "&token_access_type=offline"
    )
    print("Opening Dropbox authorization page in your browser...")
    print(f"  If it doesn't open, visit: {auth_url}")
    print()
    webbrowser.open(auth_url)

    print("Steps:")
    print("  1. Click 'Allow' to authorize the SiftStack app")
    print("  2. Copy the authorization code shown on the next page")
    print()
    code = input("Paste the auth code here, then press Enter: ").strip()

    if not code:
        print("ERROR: No code provided. Aborting.")
        return 1

    resp = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={"code": code, "grant_type": "authorization_code"},
        auth=(config.DROPBOX_APP_KEY, config.DROPBOX_APP_SECRET),
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed ({resp.status_code}): {resp.text}")
        return 1

    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print(f"ERROR: No refresh_token in response: {data}")
        return 1

    print()
    print("✅ SUCCESS — new refresh token minted.")
    print()
    print("Next steps:")
    print(f"  1. Open .env and replace the DROPBOX_REFRESH_TOKEN line with:")
    print()
    print(f"     DROPBOX_REFRESH_TOKEN={refresh_token}")
    print()
    print("  2. Re-run: src/main.py analyze --address '...' --share")
    return 0


if __name__ == "__main__":
    sys.exit(main())
