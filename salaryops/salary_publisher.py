#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Salary Publisher via Microsoft Graph (Confidential client, delegated auth)
--------------------------------------------------------------------------
- Uses MSAL SerializableTokenCache (no manual refresh-token handling)
- Tries acquire_token_silent() first; falls back to local loopback auth code
- Wraps Graph calls with retry/backoff (429/5xx) and auto-refresh on 401
- Publishes monthly salary PDFs to active workers based on a JSON config

CONFIG (JSON) structure example:
{
  "salaryops": {
    "base_folder": "~/company",                 # root path for workers' folders
    "workers_folder": "workers",                # subfolder with worker directories
    "worker_salary_folder": "salary",           # salary subfolder inside each worker folder
    "salary_send_test": false,                  # true = dry-run (no emails)
    "workers_send_list": {                      # optional include/exclude; empty or missing = all active
      "include": ["302615372"],
      "exclude": ["60176187"]
    },
    "hebrew_month_names": true,                 # if true, use Hebrew month names in subject/body
    "workers": {
      "302615372": {
        "active": true,
        "prefix": "moran-hilo",
        "name": "Moran Hilo",
        "name_he": "מורן",
        "email": "user@example.com",
        "folder": "moran.hilo"
      },
      "123456789": {
        "active": false,
        "prefix": "john-doe",
        "name": "John Doe",
        "name_he": "ג׳ון",
        "email": "john@example.com",
        "folder": "john.doe"
      }
    }
  }
}

USAGE
-----
export MS_CLIENT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export MS_CLIENT_SECRET="super-secret"
python3 salary_publisher.py --config /path/to/config.json

On first run, a browser will open for sign-in and consent.
Subsequent runs should be hands-off (token cache persisted to ~/.msal_token_cache.bin).
"""

import argparse
import base64
import datetime
import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
from mimetypes import guess_extension
import PyPDF2

import httpx
import msal
from dotenv import load_dotenv
from dateutil import relativedelta

# =========================
# Graph / Auth constants
# =========================
AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal Microsoft accounts
REDIRECT_URI = (
    "http://localhost:53135/callback"  # must be added to your app's redirect URIs
)
TOKEN_CACHE_PATH = Path.home() / ".msal_token_cache.bin"
MS_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SCOPES_DEFAULT = ["Mail.Read", "Mail.Send"]

MS_GRAPH_ME = f"{MS_GRAPH_BASE_URL}/me"
MS_GRAPH_ME_MSGS = f"{MS_GRAPH_BASE_URL}/me/messages"
MS_GRAPH_ME_FOLDERS = f"{MS_GRAPH_BASE_URL}/me/mailFolders"
MS_GRAPH_SEND_MAIL = f"{MS_GRAPH_BASE_URL}/me/sendMail"


# =========================
# Local loopback auth-code server
# =========================
class _AuthCodeHandler(BaseHTTPRequestHandler):
    auth_code: Optional[str] = None

    def do_GET(self):
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
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format, *args):
        # silence server logs
        pass


def _get_auth_code_via_local_server(auth_url: str, timeout_sec: int = 180) -> str:
    import webbrowser

    server = HTTPServer(("127.0.0.1", 53135), _AuthCodeHandler)
    webbrowser.open(auth_url)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    start = time.time()
    while _AuthCodeHandler.auth_code is None and (time.time() - start) < timeout_sec:
        time.sleep(0.1)

    server.server_close()
    if code := _AuthCodeHandler.auth_code:
        return code
    else:
        raise TimeoutError("Timed out waiting for authorization code.")


# =========================
# MSAL-based auth wrapper
# =========================
class MsGraphAuth:
    def __init__(
        self, client_id: str, client_secret: str, scopes: List[str] = SCOPES_DEFAULT
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes

        self.cache = msal.SerializableTokenCache()
        if TOKEN_CACHE_PATH.exists():
            self.cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))

        self.app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=AUTHORITY,
            token_cache=self.cache,
        )

    def _persist_cache(self) -> None:
        if self.cache.has_state_changed:
            TOKEN_CACHE_PATH.write_text(self.cache.serialize(), encoding="utf-8")

    def acquire_token(self) -> str:
        # Try silent first
        accounts = self.app.get_accounts()
        result = None
        if accounts:
            result = self.app.acquire_token_silent(self.scopes, account=accounts[0])
        if result and "access_token" in result:
            self._persist_cache()
            return result["access_token"]

        # Fallback: Interactive (auth code) via local loopback
        auth_url = self.app.get_authorization_request_url(
            self.scopes, redirect_uri=REDIRECT_URI
        )
        code = _get_auth_code_via_local_server(auth_url)
        result = self.app.acquire_token_by_authorization_code(
            code, scopes=self.scopes, redirect_uri=REDIRECT_URI
        )

        if "access_token" not in result:
            raise RuntimeError(f"Auth failed: {json.dumps(result, indent=2)}")

        self._persist_cache()
        return result["access_token"]


# =========================
# Graph client with retry & auto-refresh
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
        resp = None
        for attempt in range(self.max_retries):
            resp = self._client.request(method, url, headers=self._headers(), **kwargs)

            # Auto-refresh once on 401
            if resp.status_code == 401 and attempt == 0:
                self._access_token = self.auth.acquire_token()
                continue

            # Backoff on 429/5xx
            if resp.status_code in (429, 500, 502, 503, 504):
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) if ra and ra.isdigit() else backoff
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 8.0)
                continue

            return resp

        if resp is None:
            raise RuntimeError("No response received from HTTP request.")
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


HEBREW_MONTHS = [
    "",
    "ינואר",
    "פברואר",
    "מרץ",
    "אפריל",
    "מאי",
    "יוני",
    "יולי",
    "אוגוסט",
    "ספטמבר",
    "אוקטובר",
    "נובמבר",
    "דצמבר",
]


def format_month_year(dt: datetime.datetime, hebrew: bool = True) -> str:
    if hebrew:
        return f"{HEBREW_MONTHS[dt.month]} {dt.year}"
    # fallback: English month names
    import calendar

    return f"{calendar.month_name[dt.month]} {dt.year}"


def parse_workers_send_list(value: Any) -> tuple[set[str], set[str]]:
    include: set[str] = set()
    exclude: set[str] = set()
    if isinstance(value, dict):
        include = {str(x) for x in (value.get("include") or [])}
        exclude = {str(x) for x in (value.get("exclude") or [])}
    else:
        include = {str(x) for x in (value or [])}
    return include, exclude


# =========================
# Email manager (focused on /me endpoints)
# =========================
class EmailManager:
    def __init__(self, graph: GraphClient):
        self.graph = graph

    def me(self) -> Dict[str, Any]:
        r = self.graph.get(MS_GRAPH_ME)
        r.raise_for_status()
        return r.json()

    def send_mail(
        self,
        subject: str,
        body_html: str,
        to_email: str,
        attachments: Optional[List[Path]] = None,
    ) -> None:
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

    def get_messages_by_filter(
        self,
        filter_str: str,
        folder_id: Optional[str] = None,
        fields: str = "*",
        top: int = 25,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        base = (
            f"{MS_GRAPH_ME_FOLDERS}/{folder_id}/messages" if folder_id else f"{MS_GRAPH_ME_MSGS}"
        )
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
            params = {}  # after first page, Graph encodes params in nextLink
            if url and len(results) + top > max_results:
                # limit next page size
                url += (
                    "&" if "?" in url else "?"
                ) + f"$top={max_results - len(results)}"
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
        return next(
            (
                f
                for f in self.list_folders()
                if f.get("displayName", "").lower() == name
            ),
            None,
        )

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        url = f"{MS_GRAPH_ME_MSGS}/{message_id}/attachments"
        r = self.graph.get(url)
        r.raise_for_status()
        return r.json().get("value", [])

    def download_attachment(
        self, message_id: str, attachment_id: str, dest: Path
    ) -> None:
        url = f"{MS_GRAPH_ME_MSGS}/{message_id}/attachments/{attachment_id}/$value"
        r = self.graph.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


class PDFManager:
    """PDFManager class to manage PDFs"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config: Dict[str, Any] = config
        self._root_folder: str = self._config["salaryops"]["base_folder"]
        self._salary_pdfs_folder: str = self._config["salaryops"][
            "slips_downloads_folder"
        ]
        self._workers_root: str = self._config["salaryops"]["workers_folder"]
        self._workers: Dict[str, Any] = self._config["salaryops"]["workers"]
        self._salary_folder: str = self._config["salaryops"]["worker_salary_folder"]

    def distribute_pdfs(self) -> None:
        """Distribute salary PDFs to employees"""

        # create worker folders
        self._create_worker_folders()

        # iterate sorted list of salary pdfs
        salary_pdfs_folder_path = Path(self._salary_pdfs_folder)
        for salary_pdf in sorted(salary_pdfs_folder_path.glob("*.pdf")):
            # extract salary slip pdf
            self._extract_salary_slip_pdf(salary_pdf)

    def _create_worker_folders(self) -> None:
        """Create worker folders"""

        workers_folder = Path(self._root_folder).expanduser() / self._workers_root
        workers_folder.mkdir(parents=True, exist_ok=True)

        for worker in self._workers.values():
            # skip non active workers
            if not worker["active"]:
                continue

            # create worker folder
            worker_folder = workers_folder / worker["folder"]
            worker_folder.mkdir(parents=True, exist_ok=True)

            # create worker salary folder
            worker_salary_folder = worker_folder / self._salary_folder
            worker_salary_folder.mkdir(parents=True, exist_ok=True)

    def _extract_salary_slip_pdf(self, salary_pdf: Path) -> None:
        """Extract salary slip PDF"""

        # create a PDF Reader object
        with salary_pdf.open("rb") as f:
            pdf_reader = PyPDF2.PdfReader(f)

            # iterate pages of salary pdfs
            for page_num in range(len(pdf_reader.pages)):
                self._process_pdf_page(pdf_reader, page_num)

    def _process_pdf_page(self, pdf_reader: PyPDF2.PdfReader, page_num: int) -> None:
        """Process PDF page"""

        # create a PDF Writer object for each page
        pdf_writer = PyPDF2.PdfWriter()

        # add the page to the writer object
        page = pdf_reader.pages[page_num]
        pdf_writer.add_page(page)

        # extract text from pdf
        text: str = page.extract_text()
        if text is None:
            raise ValueError("Failed to extract text")
        text = text.replace("\n", "")

        # get current month and year
        now = datetime.datetime.now()
        payment_date = now + relativedelta.relativedelta(months=-1)

        for worker_id in self._workers.keys():
            if worker_id not in text:
                continue

            # worker found, initialize worker data
            worker: Dict[str, Any] = self._workers[worker_id]

            # skip non active workers
            if not worker["active"]:
                continue

            # worker found, create salary file name
            salary_file_name = (
                f"{worker['prefix']}-{worker_id}-"
                f"{payment_date.month}-{payment_date.year}.pdf"
            )

            # create worker salary folder
            worker_salary_folder: Path = self._ensure_worker_salary_folder(worker_id)

            print(f"Creating salary slip for {worker_id}: {salary_file_name}")

            # create salary slip pdf
            salary_file = worker_salary_folder / salary_file_name
            with salary_file.open("wb") as f:
                pdf_writer.write(f)

    def _ensure_worker_salary_folder(self, worker_id: str) -> Path:
        """Create worker salary folder"""
        workers_folder: Path = Path(self._root_folder).expanduser() / self._workers_root
        worker_folder: Path = workers_folder / self._workers[worker_id]["folder"]
        worker_salary_folder: Path = worker_folder / self._salary_folder
        worker_salary_folder.mkdir(parents=True, exist_ok=True)
        return worker_salary_folder

    def _extract_text_from_pdf(self, pdf_path: str) -> str:
        """Extract text from a PDF file"""
        # Open the PDF file in read-binary mode
        with open(pdf_path, "rb") as file:
            reader = PyPDF2.PdfReader(file)
            text = "".join(page.extract_text() for page in reader.pages)
        return text


# =========================
# Salary publisher
# =========================
class SalaryPublisher:
    def __init__(self, email_mgr: EmailManager, config: Dict[str, Any]):
        if "salaryops" not in config:
            raise ValueError("Config must contain 'salaryops' key")
        self.cfg = config["salaryops"]
        self.email_mgr = email_mgr

        self.base_folder = Path(self.cfg["base_folder"]).expanduser()
        self.workers_root = self.cfg["workers_folder"]
        self.worker_salary_folder = self.cfg["worker_salary_folder"]
        self.salary_send_test = bool(self.cfg.get("salary_send_test", False))
        selection = self.cfg.get("workers_send_list", [])
        self.allowed_workers, self.excluded_workers = parse_workers_send_list(selection)
        self.hebrew_month_names = bool(self.cfg.get("hebrew_month_names", True))
        self.workers: Dict[str, Any] = self.cfg["workers"]

    def _prev_month(self) -> datetime.datetime:
        now = datetime.datetime.now()
        prev_month = now + relativedelta.relativedelta(months=-1)
        # choose day 1 for naming purposes; we only need month & year
        return datetime.datetime(prev_month.year, prev_month.month, 1)

    def _salary_filename(self, worker_id: str) -> str:
        prev = self._prev_month()
        w = self.workers[worker_id]
        prefix = w["prefix"]
        return f"{prefix}-{worker_id}-{prev.month}-{prev.year}.pdf"

    def _salary_path(self, worker_id: str) -> Path:
        w = self.workers[worker_id]
        worker_dir = (
            self.base_folder
            / self.workers_root
            / w["folder"]
            / self.worker_salary_folder
        )
        worker_dir.mkdir(parents=True, exist_ok=True)
        return worker_dir / self._salary_filename(worker_id)

    def _subject_and_body(self, worker_id: str) -> tuple[str, str]:
        prev = self._prev_month()
        month_year = format_month_year(prev, hebrew=self.hebrew_month_names)
        w = self.workers[worker_id]
        name_he = w.get("name_he") or w.get("name") or ""

        title = "תלוש שכר עבור"
        subject = f"{title} {month_year}"
        body_he = (
            f"שלום {name_he},<br><br>"
            f"מצורף תלוש שכר עבור {month_year}.<br><br>"
            f"בברכה,<br>אינה"
        )
        html = f"<div dir='rtl' style='font-family:Arial,Helvetica,sans-serif;font-size:14px'>{body_he}</div>"
        return subject, html

    def _should_send_worker(self, worker_id: str, worker: Dict[str, Any]) -> bool:
        if not worker.get("active", False):
            return False
        if self.allowed_workers and worker_id not in self.allowed_workers:
            return False
        if worker_id in self.excluded_workers:
            return False
        return True

    def publish(self) -> None:
        # who am I
        me = self.email_mgr.me()
        print(f"Signed in as: {me.get('userPrincipalName', me.get('mail', 'unknown'))}")

        count_total = 0
        count_sent = 0
        count_skipped = 0
        count_missing = 0

        for worker_id, worker in self.workers.items():
            count_total += 1
            if not self._should_send_worker(worker_id, worker):
                print(
                    f"[skip] Worker {worker_id} ({worker.get('name')}) not eligible (inactive / not selected)."
                )
                count_skipped += 1
                continue

            salary_path = self._salary_path(worker_id)
            if not salary_path.exists():
                print(f"[miss] Salary file not found for {worker_id}: {salary_path}")
                count_missing += 1
                continue

            subject, body_html = self._subject_and_body(worker_id)
            to_email = worker["email"]

            if self.salary_send_test:
                print(
                    f"[dry-run] Would send '{salary_path.name}' to {to_email} (worker {worker_id})."
                )
                count_sent += 1
                continue

            try:
                self.email_mgr.send_mail(
                    subject, body_html, to_email, attachments=[salary_path]
                )
                print(f"[sent] {salary_path.name} -> {to_email}")
                count_sent += 1
            except httpx.HTTPError as e:
                print(f"[error] Failed to send to {to_email}: {e}")

        print(
            f"Done. total={count_total}, sent={count_sent}, skipped={count_skipped}, missing={count_missing}"
        )


def download_salary_pdfs(config: Dict[str, Any], email_manager: EmailManager) -> None:
    """Download salary PDFs from emails"""

    # Create the folder to store the downloaded salary PDFs
    base_folder = config["salaryops"]["base_folder"]
    downloads = config["salaryops"]["slips_downloads_folder"]
    downloads_folder = Path(base_folder).expanduser() / downloads
    downloads_folder.mkdir(parents=True, exist_ok=True)

    print(f"Downloads folder: {downloads_folder}")

    # clean downloads folder
    for file in downloads_folder.glob("sal-*.pdf"):
        try:
            file.unlink()
        except Exception as e:
            print(f"Warning: Could not delete {file}: {e}")

    # create query filter
    from datetime import datetime, timezone

    # Calculate first day of current month at midnight UTC
    first_of_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    received_date_str = first_of_month.strftime("%Y-%m-%dT%H:%M:%SZ")

    sal_filter = (
        r"(from/emailAddress/address eq 'yael@damsalem.co.il' or "
        r"from/emailAddress/address eq 'batya@damsalem.co.il') and "
        r"contains(subject, 'שכר') and "
        r"hasAttachments eq true and "
        fr"receivedDateTime ge {received_date_str}"
    )

    messages = email_manager.get_messages_by_filter(sal_filter)  # type: ignore
    print(f"got {len(messages)} messages with salary slips...")

    for message in messages:  # type: ignore
        import re

        def sanitize_filename(filename: str) -> str:
            # Remove or replace invalid filename characters for most OSes
            # Windows: <>:"/\|?* and non-printable chars
            # Unix: /
            # Replace with underscore
            return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", filename)

        import logging
        attachments = email_manager.get_attachments(message["id"])
        for attachment in attachments:
            attachment_extension = guess_extension(
                attachment["contentType"], strict=True
            )
            if attachment_extension is None:
                logging.warning(
                    f"Unknown content type '{attachment['contentType']}' for attachment '{attachment.get('name', 'unknown')}'."
                )
                # Try to preserve original extension if present
                original_name = attachment.get("name", "")
                original_ext = Path(original_name).suffix
                attachment_extension = original_ext or ".bin"
            if attachment_extension is None:
                attachment_extension = ".bin"
            attachment_name = (
                f"sal-{attachment['lastModifiedDateTime']}{attachment_extension}"
            )
            attachment_name = sanitize_filename(attachment_name)
            email_manager.download_attachment(
                message["id"], attachment["id"], downloads_folder / attachment_name
            )


def distribute_salary_pdfs(config: Dict[str, Any], pdf_manager: PDFManager) -> None:
    """Distribute salary PDFs to employees"""

    print("Distribute salary PDFs to employees...")

    # Create the folder for workers
    base_folder: str = config["salaryops"]["base_folder"]
    workers_folder: Path = (
        Path(base_folder).expanduser() / config["salaryops"]["workers_folder"]
    )
    workers_folder.mkdir(parents=True, exist_ok=True)

    print(f"workers folder: {workers_folder}")

    pdf_manager.distribute_pdfs()


# =========================
# CLI
# =========================
def main():
    parser = argparse.ArgumentParser(
        description="Publish monthly salary PDFs via Microsoft Graph"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file as described in the module docstring",
    )
    parser.add_argument(
        "--scopes",
        default=",".join(SCOPES_DEFAULT),
        help="Comma-separated delegated scopes (default: Mail.Read,Mail.Send)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout seconds (default: 30)"
    )
    parser.add_argument(
        "--retries", type=int, default=4, help="Max retries for 429/5xx (default: 4)"
    )
    args = parser.parse_args()

    # Load environment variables from .env file
    load_dotenv()
    client_id = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: Please set MS_CLIENT_ID and MS_CLIENT_SECRET environment variables."
        )
        sys.exit(1)

    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"ERROR: Config file not found: {cfg_path}")
        sys.exit(1)

    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Failed to parse config JSON: {e}")
        sys.exit(1)

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    auth = MsGraphAuth(client_id, client_secret, scopes=scopes)
    graph = GraphClient(auth, timeout=args.timeout, max_retries=args.retries)
    email = EmailManager(graph)
    pdf_manager = PDFManager(config)

    print("Start downloading salary pdfs....")
    download_salary_pdfs(config, email)

    print("Start distributing salary pdfs...")
    distribute_salary_pdfs(config, pdf_manager)

    publisher = SalaryPublisher(email, config)
    publisher.publish()


if __name__ == "__main__":
    main()
