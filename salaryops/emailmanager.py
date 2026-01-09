""" Email manager module """

import base64
import calendar
import datetime
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

import dateutil.relativedelta
import httpx
from msgraphhelper import (
    MS_GRAPH_ME,
    MS_GRAPH_ME_FOLDERS,
    MS_GRAPH_ME_MSGS,
    MS_GRAPH_SEND_MAIL,
    GraphClient,
    create_file_attachment,
    get_mime_type,
)


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


class EmailManager:
    """EmailManager class to manage emails"""

    def __init__(
        self, config: Dict[str, Any], app_id: str, secret: str, scopes: List[str]
    ) -> None:
        self.access_token = ""
        self.msgraphhelper = MsGraphHelper(app_id, secret, scopes)
        self._config = config
        self._root_folder: str = self._config["salaryops"]["base_folder"]
        self._workers: Dict[str, Any] = self._config["salaryops"]["workers"]
        self._workers_root: str = self._config["salaryops"]["workers_folder"]
        self._salary_folder: str = self._config["salaryops"]["worker_salary_folder"]
        selection = self._config["salaryops"].get("workers_send_list", [])
        (
            self._workers_include,
            self._workers_exclude,
        ) = self._parse_workers_send_list(selection)

    def _parse_workers_send_list(self, value: Any) -> tuple[set[str], set[str]]:
        include: set[str] = set()
        exclude: set[str] = set()
        if isinstance(value, dict):
            include = {str(x) for x in (value.get("include") or [])}
            exclude = {str(x) for x in (value.get("exclude") or [])}
        else:
            include = {str(x) for x in (value or [])}
        return include, exclude

    def _should_send_worker(self, worker_id: str) -> bool:
        if self._workers_include and worker_id not in self._workers_include:
            return False
        if worker_id in self._workers_exclude:
            return False
        return True

    def publish_salary_pdfs(self) -> None:
        """Publish salary PDFs using email"""

        # iterate over workers
        for worker_id, worker in self._workers.items():
            # skip non active workers
            if not worker["active"]:
                continue

            # create worker salary file name
            salary_file_name: str = self._create_salary_file_name(worker_id)

            # create worker salary folder path
            workers_folder: Path = (
                Path(self._root_folder).expanduser() / self._workers_root
            )
            worker_folder: Path = workers_folder / self._workers[worker_id]["folder"]
            worker_salary_folder: Path = worker_folder / self._salary_folder
            worker_salary_folder.mkdir(parents=True, exist_ok=True)

            # check if salary file exists
            salary_file_path: Path = worker_salary_folder / salary_file_name
            if not salary_file_path.exists():
                continue

            # send email with salary slip
            self._send_email(worker_id, salary_file_path)

    def _create_salary_file_name(self, worker_id: str) -> str:
        """Create salary file name"""

        # get current month and year
        now = datetime.datetime.now()
        payment_date = now + dateutil.relativedelta.relativedelta(months=-1)

        worker: Dict[str, Any] = self._workers[worker_id]
        salary_file_name: str = (
            f"{worker['prefix']}-{worker_id}-"
            f"{payment_date.month}-{payment_date.year}.pdf"
        )

        return salary_file_name

    def _send_email(self, worker_id: str, salary_file_path: Path) -> None:
        """Send email with salary slip"""

        # get worker email
        worker: Dict[str, Any] = self._workers[worker_id]
        name_he = worker["name_he"]
        worker_email: str = worker["email"]
        # worker_email = "vgeshiktor@gmail.com"

        # email details
        now = datetime.datetime.now()
        payment_date = now + dateutil.relativedelta.relativedelta(months=-1)
        title: str = "תלוש שכר עבור"
        subject: str = (
            f"{title} {calendar.month_name[payment_date.month]} {payment_date.year}"
        )
        body: str = (
            f"שלום {name_he},\n\nמצורף תלוש שכר עבור "
            f"{calendar.month_name[payment_date.month]} "
            f"{payment_date.year}.\n\nבברכה,\nאינה"
        )
        attachment_path = salary_file_path
        attachments = [create_attachment(attachment_path)]

        # create email message
        message = {
            "message": self.draft_message_body(subject, body, worker_email, attachments)
        }

 
        if not self._config["salaryops"]["salary_send_test"]:
            if self._should_send_worker(worker_id):
                # real send

                # send email using ms graph api
                headers = self.get_auth_headers()
                response = httpx.post(MS_GRAPH_ME_SEND_EMAIL_EP, headers=headers, json=message)
                response.raise_for_status()

                print(
                    f"Email to {worker_email} "
                    f"with attachment "
                    f"{attachment_path.name} "
                    f"sent successfully!"
                )
            else:
                print(
                    f"Skipping worker with id: {worker_id} "
                    f"worker name: {worker["name"]}"
                )
        else:
            # draft send
            print(
                f"Draft send email to {worker_email} "
                f"with attachment "
                f"{attachment_path.name} "
                f"completed successfully!"
            )

    def draft_message_body(
        self, subject: str, body: str, to_email: str, attachments: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Draft email message body"""

        return {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": f"<div dir='rtl'>{body}</div>",
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": to_email,
                    },
                },
            ],
            "attachments": attachments,
        }

    def search_folder(self, folder_name: str = "drafts") -> Any:
        """search for folder id"""
        headers = self.get_auth_headers()
        folders: List[Dict[str, Any]] = []
        next_link = MS_GRAPH_ME_FOLDERS_EP

        while next_link:
            response = httpx.get(next_link, headers=headers)
            response.raise_for_status()
            json_response = response.json()
            folders.extend(json_response.get("value", []))
            next_link = json_response.get("@odata.nextLink", None)

        for folder in folders:
            print(folder["displayName"].lower())
        return next(
            (
                folder
                for folder in folders
                if folder["displayName"].lower() == folder_name
            ),
            None,
        )


def get_mime_type(file_path: str) -> str:
    """Get mime type of a file"""
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or "application/octet-stream"


def create_attachment(file_path: Path) -> Dict[str, str]:
    """Create email attachment"""
    encoded_content = base64.b64encode(file_path.read_bytes()).decode("utf-8")

    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": Path(file_path).name,
        "contentType": get_mime_type(str(file_path)),
        "contentBytes": encoded_content,
    }
