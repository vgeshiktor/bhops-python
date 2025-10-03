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
    def log_message(self, fmt, *args):
        return

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

# =========================
# Email Manager (uses GraphClient)
# =========================
class EmailManager:
    def __init__(self, graph: GraphClient):
        self.graph = graph

    def me(self) -> Dict[str, Any]:
        r = self.graph.get(MS_GRAPH_ME)
        r.raise_for_status()
        return r.json()

    def get_messages_by_filter(
        self,
        filter_str: str,
        folder_id: Optional[str] = None,
        fields: str = "*",
        top: int = 25,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        base = f"{MS_GRAPH_ME_MSGS}" if not folder_id else f"{MS_GRAPH_ME_FOLDERS}/{folder_id}/messages"
        params: Dict[str, Any] = {"$select": fields, "$top": min(top, max_results)}
        if filter_str:
            params["$filter"] = filter_str

        results: List[Dict[str, Any]] = []
        url = base
        while url and len(results) < max_results:
            r = self.graph.get(url, params=params)
            if r.status_code != 200:
                raise httpx.RequestError(f"Failed to retrieve emails: {r.text}")
            payload = r.json()
            batch = payload.get("value", [])
            results.extend(batch)
            url = payload.get("@odata.nextLink")
            params = None  # after first page, Graph encodes params in nextLink
            if url and len(results) + top > max_results:
                # limit next page size
                url += ("&" if "?" in url else "?") + f"$top={max_results - len(results)}"
        return results[:max_results]

    def list_folders(self) -> List[Dict[str, Any]]:
        url = MS_GRAPH_ME_FOLDERS
        all_folders: List[Dict[str, Any]] = []
        while url:
            r = self.graph.get(url)
            r.raise_for_status()
            data = r.json()
            all_folders.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return all_folders

    def find_folder_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        name = name.lower().strip()
        for f in self.list_folders():
            if f.get("displayName", "").lower() == name:
                return f
        return None

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        url = f"{MS_GRAPH_ME_MSGS}/{message_id}/attachments"
        r = self.graph.get(url)
        r.raise_for_status()
        return r.json().get("value", [])

    def download_attachment(self, message_id: str, attachment_id: str, dest: Path) -> None:
        url = f"{MS_GRAPH_ME_MSGS}/{message_id}/attachments/{attachment_id}/$value"
        r = self.graph.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)

    def send_mail(self, subject: str, body_html: str, to_email: str, attachments: Optional[List[Path]] = None) -> None:
        atts = [create_file_attachment(p) for p in (attachments or [])]
        message = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": atts,
            }
        }
        r = self.graph.post(MS_GRAPH_SEND_MAIL, json=message)
        r.raise_for_status()

# =========================
# Example usage
# =========================
def main():
    client_id = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Please set MS_CLIENT_ID and MS_CLIENT_SECRET")
        sys.exit(1)

    auth = MsGraphAuth(client_id, client_secret, SCOPES_DEFAULT)
    graph = GraphClient(auth)

    email = EmailManager(graph)

    # Who am I?
    me = email.me()
    print(f"Signed in as: {me.get('userPrincipalName')}")

    # Find Drafts folder
    drafts = email.find_folder_by_name("drafts")
    if drafts:
        print(f"Drafts folder id: {drafts['id']}")

    # Example: search recent messages from a sender (adjust filter as needed)
    msgs = email.get_messages_by_filter(
        filter_str="from/emailAddress/address eq 'someone@example.com'",
        top=10,
        max_results=10,
    )
    print(f"Found {len(msgs)} messages.")

    # Example: send mail with an attachment
    # email.send_mail(
    #     subject="שלום",
    #     body_html="<div dir='rtl'>תלוש מצורף</div>",
    #     to_email="user@example.com",
    #     attachments=[Path("/path/to/file.pdf")],
    # )

if __name__ == "__main__":
    main()
