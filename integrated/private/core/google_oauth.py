from __future__ import annotations
from pathlib import Path
from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

def authorization_url(client_secret_file: str, redirect_uri: str, state: str):
    flow = Flow.from_client_secrets_file(client_secret_file, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return url

def exchange_code(client_secret_file: str, redirect_uri: str, state: str, code: str):
    flow = Flow.from_client_secrets_file(client_secret_file, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    return flow.credentials
