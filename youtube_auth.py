import json
import logging
import os
from pathlib import Path
from typing import Optional

from config.paths import get_nova_data_dir
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

client_secret_env = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_PATH")
if client_secret_env:
    CLIENT_SECRET_PATH = Path(client_secret_env)
else:
    CLIENT_SECRET_PATH = get_nova_data_dir() / "client_secret.json"

TOKEN_PATH = Path(os.getenv("GOOGLE_OAUTH_TOKEN_PATH", str(get_nova_data_dir() / "token.json")))
TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)


def _get_credentials() -> Optional[Credentials]:
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            if creds and creds.valid:
                return creds
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(GoogleRequest())
                TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                return creds
        except Exception as exc:
            logger.warning("Failed to load cached YouTube credentials: %s", exc)

    return None


def ensure_youtube_auth() -> Optional[Credentials]:
    creds = _get_credentials()
    if creds is not None:
        return creds

    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            "Google OAuth client secret not found. Set GOOGLE_OAUTH_CLIENT_SECRET_PATH "
            f"or place client_secret.json in {get_nova_data_dir()}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved YouTube OAuth token to %s", TOKEN_PATH)
    return creds


if __name__ == "__main__":
    print("Starting YouTube authentication...")
    creds = ensure_youtube_auth()
    print("YouTube authentication successful!")
