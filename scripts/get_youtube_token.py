"""One-time OAuth flow: browser login as the channel account, print + persist a refresh
token. Run this logged into the *channel* Google account, not the Cloud/Gemini one
(DESIGN.md §3.1 #8 — the two are deliberately separate accounts).

Usage: uv run python scripts/get_youtube_token.py [client_secret.json] [--token-out token.json]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

#: youtube.upload alone can't set thumbnails/playlists/metadata after upload — force-ssl
#: is required for that (DESIGN.md #19).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_secret", type=Path, nargs="?", default=Path("client_secret.json"))
    parser.add_argument("--token-out", type=Path, default=Path("token.json"))
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), scopes=SCOPES)
    credentials = flow.run_local_server(port=0)

    args.token_out.write_text(credentials.to_json(), encoding="utf-8")
    print(f"refresh token (copy into .env as YOUTUBE_REFRESH_TOKEN):\n{credentials.refresh_token}")
    print(f"full credentials written to {args.token_out} — keep this out of backups/cloud sync")


if __name__ == "__main__":
    main()
