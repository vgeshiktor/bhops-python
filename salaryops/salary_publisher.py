#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Salary Publisher via Microsoft Graph mail APIs.
- Uses MSAL `PublicClientApplication` with a persistent token cache
- Tries acquire_token_silent() first and fails fast unattended when auth is missing
- Supports one-time device-code bootstrap auth when explicitly enabled
- Retries Graph calls on 401 refresh and 429/5xx backoff
- Publishes monthly salary PDFs to active workers based on a JSON config

CONFIG (JSON) structure example:
{
  "salaryops": {
    "base_folder": "~/company",
    "workers_folder": "workers",
    "worker_salary_folder": "salary",
    "salary_send_test": false,
    "workers_send_list": {
      "include": ["302615372"],
      "exclude": ["60176187"]
    },
    "hebrew_month_names": true,
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
export MS_AUTHORITY="consumers"
export MS_TOKEN_CACHE_PATH="$HOME/.msal_token_cache.bin"
python3 salary_publisher.py --config /path/to/config.json

Bootstrap once with:
python3 salary_publisher.py --config /path/to/config.json --interactive-auth

Subsequent runs should be hands-off and reuse the same token cache path.
"""

import argparse
import base64
import datetime
import json
import logging
import mimetypes
import os
import sys
import time
from mimetypes import guess_extension
from pathlib import Path
from typing import Any, Dict, List, Optional

import PyPDF2
import msal
import requests
from dateutil import relativedelta
from dotenv import load_dotenv


AUTHORITY_BASE_URL = "https://login.microsoftonline.com"
DEFAULT_AUTHORITY = "consumers"
TOKEN_CACHE_PATH = Path.home() / ".msal_token_cache.bin"
MS_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SCOPES_DEFAULT = ["Mail.Read", "Mail.Send"]
MSAL_RESERVED_SCOPES = {"openid", "profile", "offline_access"}

MS_GRAPH_ME = f"{MS_GRAPH_BASE_URL}/me"
MS_GRAPH_ME_MSGS = f"{MS_GRAPH_BASE_URL}/me/messages"
MS_GRAPH_ME_FOLDERS = f"{MS_GRAPH_BASE_URL}/me/mailFolders"
MS_GRAPH_SEND_MAIL = f"{MS_GRAPH_BASE_URL}/me/sendMail"


def normalize_msal_scopes(scopes: Optional[List[str]]) -> List[str]:
    requested = scopes or SCOPES_DEFAULT
    cleaned: List[str] = []
    removed: List[str] = []
    seen = set()
    for scope in requested:
        item = (scope or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in MSAL_RESERVED_SCOPES:
            removed.append(item)
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    if not cleaned:
        raise ValueError("At least one non-reserved Graph scope is required.")
    if removed:
        logging.debug(
            "Ignoring reserved OAuth scopes for MSAL: %s",
            ", ".join(sorted(set(removed))),
        )
    return cleaned


def _resolve_authority(authority: Optional[str]) -> str:
    candidate = (authority or DEFAULT_AUTHORITY).strip()
    if candidate.startswith("https://"):
        return candidate.rstrip("/")
    return f"{AUTHORITY_BASE_URL}/{candidate}"


def _is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class GraphClient:
    def __init__(
        self,
        client_id: str,
        authority: str = DEFAULT_AUTHORITY,
        scopes: Optional[List[str]] = None,
        token_cache_path: Optional[str] = None,
        interactive_auth: bool = False,
        timeout: float = 30.0,
        max_retries: int = 4,
    ):
        self.client_id = client_id
        self.authority = _resolve_authority(authority)
        self.scopes = normalize_msal_scopes(scopes)
        self.timeout = timeout
        self.max_retries = max_retries

        cache_candidate = (
            token_cache_path
            or os.getenv("MS_TOKEN_CACHE_PATH")
            or os.getenv("MSAL_TOKEN_CACHE_PATH")
            or str(TOKEN_CACHE_PATH)
        )
        self.token_cache_path = Path(cache_candidate).expanduser()
        self.cache = msal.SerializableTokenCache()
        if self.token_cache_path.exists():
            try:
                self.cache.deserialize(
                    self.token_cache_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                logging.warning(
                    "Failed to read token cache (%s): %s", self.token_cache_path, exc
                )

        self.app = msal.PublicClientApplication(
            self.client_id,
            authority=self.authority,
            token_cache=self.cache,
        )
        self.session = requests.Session()
        self.token = self._acquire_token(interactive_auth)
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _persist_cache(self) -> None:
        if not self.cache.has_state_changed:
            return
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache_path.write_text(self.cache.serialize(), encoding="utf-8")

    def _acquire_token_silent(self) -> Optional[str]:
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        result = self.app.acquire_token_silent(self.scopes, account=accounts[0])
        if result and "access_token" in result:
            self._persist_cache()
            return result["access_token"]
        return None

    def _acquire_token(self, interactive: bool) -> str:
        token = self._acquire_token_silent()
        if token:
            return token
        if not interactive:
            raise RuntimeError(
                "AUTH_REQUIRED: No cached token available. Run once with "
                "--interactive-auth to authorize."
            )

        flow = self.app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise RuntimeError("MSAL device flow init failed")

        print("== Device Code auth ==")
        print(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        if result.get("error") == "expired_token":
            print("Device code expired before authorization; retrying with a fresh code...")
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise RuntimeError("MSAL device flow init failed")
            print(flow["message"])
            result = self.app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(f"MSAL failed: {result}")

        self._persist_cache()
        return result["access_token"]

    def _refresh_access_token(self) -> bool:
        token = self._acquire_token_silent()
        if not token:
            return False
        self.token = token
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        return True

    def _retry_delay_seconds(self, response: requests.Response, attempt: int) -> float:
        retry_after = (response.headers.get("Retry-After") or "").strip()
        if retry_after:
            try:
                value = float(retry_after)
            except ValueError:
                value = 0.0
            if value > 0:
                return value
        return float(min(30, 2**attempt))

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        req_headers = dict(headers or {})
        refreshed = False
        attempt = 0
        while True:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                headers=req_headers,
                timeout=self.timeout,
                json=json,
            )
            if response.status_code == 401 and not refreshed:
                refreshed = True
                if self._refresh_access_token():
                    continue
            if (
                response.status_code in {429, 500, 502, 503, 504}
                and attempt < self.max_retries
            ):
                time.sleep(self._retry_delay_seconds(response, attempt))
                attempt += 1
                continue
            return response

    def get(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        return self._request("GET", url=url, params=params)

    def post(
        self, url: str, json: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        return self._request("POST", url=url, json=json)


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
            f"{MS_GRAPH_ME_FOLDERS}/{folder_id}/messages"
            if folder_id
            else f"{MS_GRAPH_ME_MSGS}"
        )
        params: Dict[str, Any] = {"$select": fields, "$top": min(top, max_results)}
        if filter_str:
            params["$filter"] = filter_str

        results: List[Dict[str, Any]] = []
        url = base
        while url and len(results) < max_results:
            r = self.graph.get(url, params=params)
            if r.status_code != 200:
                raise RuntimeError(f"Failed to retrieve emails: {r.text}")
            payload = r.json()
            batch = payload.get("value", [])
            results.extend(batch)
            url = payload.get("@odata.nextLink")
            params = {}  # after first page, Graph encodes params in nextLink
            if url and len(results) + top > max_results:
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

        self._create_worker_folders()
        salary_pdfs_folder_path = Path(self._salary_pdfs_folder)
        for salary_pdf in sorted(salary_pdfs_folder_path.glob("*.pdf")):
            self._extract_salary_slip_pdf(salary_pdf)

    def _create_worker_folders(self) -> None:
        """Create worker folders"""

        workers_folder = Path(self._root_folder).expanduser() / self._workers_root
        workers_folder.mkdir(parents=True, exist_ok=True)

        for worker in self._workers.values():
            if not worker["active"]:
                continue
            worker_folder = workers_folder / worker["folder"]
            worker_folder.mkdir(parents=True, exist_ok=True)
            worker_salary_folder = worker_folder / self._salary_folder
            worker_salary_folder.mkdir(parents=True, exist_ok=True)

    def _extract_salary_slip_pdf(self, salary_pdf: Path) -> None:
        """Extract salary slip PDF"""

        with salary_pdf.open("rb") as f:
            pdf_reader = PyPDF2.PdfReader(f)
            for page_num in range(len(pdf_reader.pages)):
                self._process_pdf_page(pdf_reader, page_num)

    def _process_pdf_page(self, pdf_reader: PyPDF2.PdfReader, page_num: int) -> None:
        """Process PDF page"""

        pdf_writer = PyPDF2.PdfWriter()
        page = pdf_reader.pages[page_num]
        pdf_writer.add_page(page)

        text: str = page.extract_text()
        if text is None:
            raise ValueError("Failed to extract text")
        text = text.replace("\n", "")

        now = datetime.datetime.now()
        payment_date = now + relativedelta.relativedelta(months=-1)

        for worker_id in self._workers.keys():
            if worker_id not in text:
                continue

            worker: Dict[str, Any] = self._workers[worker_id]
            if not worker["active"]:
                continue

            salary_file_name = (
                f"{worker['prefix']}-{worker_id}-"
                f"{payment_date.month}-{payment_date.year}.pdf"
            )
            worker_salary_folder: Path = self._ensure_worker_salary_folder(worker_id)

            print(f"Creating salary slip for {worker_id}: {salary_file_name}")

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

        with open(pdf_path, "rb") as file:
            reader = PyPDF2.PdfReader(file)
            text = "".join(page.extract_text() for page in reader.pages)
        return text


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
        html = (
            "<div dir='rtl' style='font-family:Arial,Helvetica,sans-serif;"
            f"font-size:14px'>{body_he}</div>"
        )
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
                    f"[skip] Worker {worker_id} ({worker.get('name')}) not eligible "
                    "(inactive / not selected)."
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
                    f"[dry-run] Would send '{salary_path.name}' to {to_email} "
                    f"(worker {worker_id})."
                )
                count_sent += 1
                continue

            try:
                self.email_mgr.send_mail(
                    subject, body_html, to_email, attachments=[salary_path]
                )
                print(f"[sent] {salary_path.name} -> {to_email}")
                count_sent += 1
            except requests.RequestException as e:
                print(f"[error] Failed to send to {to_email}: {e}")

        print(
            f"Done. total={count_total}, sent={count_sent}, "
            f"skipped={count_skipped}, missing={count_missing}"
        )


def download_salary_pdfs(config: Dict[str, Any], email_manager: EmailManager) -> None:
    """Download salary PDFs from emails"""

    base_folder = config["salaryops"]["base_folder"]
    downloads = config["salaryops"]["slips_downloads_folder"]
    downloads_folder = Path(base_folder).expanduser() / downloads
    downloads_folder.mkdir(parents=True, exist_ok=True)

    print(f"Downloads folder: {downloads_folder}")

    for file in downloads_folder.glob("sal-*.pdf"):
        try:
            file.unlink()
        except Exception as e:
            print(f"Warning: Could not delete {file}: {e}")

    from datetime import datetime, timezone

    first_of_month = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
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
            return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", filename)

        import logging as local_logging

        attachments = email_manager.get_attachments(message["id"])
        for attachment in attachments:
            attachment_extension = guess_extension(
                attachment["contentType"], strict=True
            )
            if attachment_extension is None:
                local_logging.warning(
                    "Unknown content type '%s' for attachment '%s'.",
                    attachment["contentType"],
                    attachment.get("name", "unknown"),
                )
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

    base_folder: str = config["salaryops"]["base_folder"]
    workers_folder: Path = (
        Path(base_folder).expanduser() / config["salaryops"]["workers_folder"]
    )
    workers_folder.mkdir(parents=True, exist_ok=True)

    print(f"workers folder: {workers_folder}")

    pdf_manager.distribute_pdfs()


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
        "--authority",
        help="Authority tenant or full authority URL (default: env or consumers)",
    )
    parser.add_argument(
        "--token-cache-path",
        help="Persistent MSAL token cache path (default: env or ~/.msal_token_cache.bin)",
    )
    parser.add_argument(
        "--interactive-auth",
        action="store_true",
        help="Enable one-time device-code bootstrap auth when no cached token exists",
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

    load_dotenv(override=True)
    client_id = os.getenv("MS_CLIENT_ID")
    if not client_id:
        print("ERROR: Please set MS_CLIENT_ID.")
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
    authority = args.authority or os.getenv("MS_AUTHORITY") or DEFAULT_AUTHORITY
    token_cache_path = (
        args.token_cache_path
        or os.getenv("MS_TOKEN_CACHE_PATH")
        or os.getenv("MSAL_TOKEN_CACHE_PATH")
    )
    interactive_auth = args.interactive_auth or _is_truthy(
        os.getenv("MS_INTERACTIVE_AUTH")
    )

    graph = GraphClient(
        client_id=client_id,
        authority=authority,
        scopes=scopes,
        token_cache_path=token_cache_path,
        interactive_auth=interactive_auth,
        timeout=args.timeout,
        max_retries=args.retries,
    )
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
