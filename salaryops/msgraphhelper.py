"""
    MsGraphHelper class to get access token using ms graph api
"""

import base64
import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx
import msal

# =========================
# Config (adjust as needed)
# =========================
AUTHORITY = "https://login.microsoftonline.com/consumers"
REDIRECT_URI = "http://localhost:53135/callback"  # must exist in your app registration
TOKEN_CACHE_PATH = Path.home() / ".msal_token_cache.bin"
MS_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SCOPES_DEFAULT = ["Mail.Read", "Mail.Send"]  # delegated scopes

# Endpoints
MS_GRAPH_ME = f"{MS_GRAPH_BASE_URL}/me"
MS_GRAPH_ME_MSGS = f"{MS_GRAPH_BASE_URL}/me/messages"
MS_GRAPH_ME_FOLDERS = f"{MS_GRAPH_BASE_URL}/me/mailFolders"
MS_GRAPH_SEND_MAIL = f"{MS_GRAPH_BASE_URL}/me/sendMail"


# =========================
# Small local redirect server
# =========================
class _AuthCodeHandler(BaseHTTPRequestHandler):
    # store code on class for simplicity
    auth_code: Optional[str] = None

    def do_GET(self):
        # Parse ?code= from /callback
        parsed = urlparse(self.path)
        if parsed.path != urlparse(REDIRECT_URI).path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        q = parse_qs(parsed.query)
        code = q.get("code", [None])[0]
        _AuthCodeHandler.auth_code = code

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"You can close this window and return to the app.")
        # shut down server soon after response
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # silence logs
    def log_message(self, format, *args):
        pass

def _get_auth_code_via_local_server(auth_url: str, timeout_sec: int = 180) -> str:
    """Open browser to auth_url and capture the authorization code on localhost."""
    import webbrowser

    server = HTTPServer(("127.0.0.1", 53135), _AuthCodeHandler)
    webbrowser.open(auth_url)

    # Serve until we receive the code (server.shutdown called in handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # Wait for the handler to set the code or timeout
    start = time.time()
    while _AuthCodeHandler.auth_code is None and (time.time() - start) < timeout_sec:
        time.sleep(0.1)

    server.server_close()
    code = _AuthCodeHandler.auth_code
    if not code:
        raise TimeoutError("Timed out waiting for authorization code.")
    return code


# =========================
# MS Graph Auth (Confidential, Delegated)
# =========================
class MsGraphAuth:
    def __init__(self, client_id: str, client_secret: str, scopes: List[str] = SCOPES_DEFAULT):
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes

        self.cache = msal.SerializableTokenCache()
        if TOKEN_CACHE_PATH.exists():
            self.cache.deserialize(TOKEN_CACHE_PATH.read_text())

        self.app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=AUTHORITY,
            token_cache=self.cache,
        )

    def _persist_cache(self) -> None:
        if self.cache.has_state_changed:
            TOKEN_CACHE_PATH.write_text(self.cache.serialize())

    def acquire_token(self) -> str:
        # 1) Try silent
        accounts = self.app.get_accounts()
        result = None
        if accounts:
            result = self.app.acquire_token_silent(self.scopes, account=accounts[0])
        if result and "access_token" in result:
            self._persist_cache()
            return result["access_token"]

        # 2) Interactive auth code (delegated) using local loopback
        auth_url = self.app.get_authorization_request_url(self.scopes, redirect_uri=REDIRECT_URI)
        code = _get_auth_code_via_local_server(auth_url)
        result = self.app.acquire_token_by_authorization_code(code, scopes=self.scopes, redirect_uri=REDIRECT_URI)

        if "access_token" not in result:
            raise RuntimeError(f"Auth failed: {json.dumps(result, indent=2)}")

        self._persist_cache()
        return result["access_token"]


# =========================
# Simple Graph client with retry & auto-refresh
# =========================
class GraphClient:
    def __init__(self, auth: MsGraphAuth, timeout: float = 30.0, max_retries: int = 4):
        self.auth = auth
        self._access_token = auth.acquire_token()
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        backoff = 0.5
        for attempt in range(self.max_retries):
            resp = self._client.request(method, url, headers=self._headers(), **kwargs)

            # 401: try refresh token once
            if resp.status_code == 401 and attempt == 0:
                self._access_token = self.auth.acquire_token()
                continue

            # 429 or 5xx: retry with backoff
            if resp.status_code in (429, 500, 502, 503, 504):
                # Try Retry-After if present
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) if ra and ra.isdigit() else backoff
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 8.0)
                continue

            return resp

        # last attempt
        return resp

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return self._request("GET", url, params=params)

    def post(self, url: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        return self._request("POST", url, json=json)

# =========================
# Helpers
# =========================
def get_mime_type(file_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or "application/octet-stream"

def create_file_attachment(file_path: Path) -> Dict[str, Any]:
    content = base64.b64encode(file_path.read_bytes()).decode("utf-8")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": file_path.name,
        "contentType": get_mime_type(str(file_path)),
        "contentBytes": content,
    }

