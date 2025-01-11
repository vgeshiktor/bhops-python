""" Email manager module """

import base64
import calendar
import datetime
import mimetypes
from pathlib import Path
from typing import Any, Dict, List

import dateutil.relativedelta
import httpx

from salaryops.msgraphhelper import (
    MS_GRAPH_ME_FOLDERS_EP,
    MS_GRAPH_ME_MSGS_EP,
    MS_GRAPH_ME_SEND_EMAIL_EP,
    MsGraphHelper,
)


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

    def login(self) -> None:
        """Login to Microsoft Graph API"""
        self.access_token = self.msgraphhelper.get_access_token()

    def get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers"""
        return {
            "Authorization": f"Bearer {self.access_token}",
        }

    def get_messages_by_filter(
        self,
        filter_str: str,
        forder_id: str | None = None,
        fields: str = "*",
        top: int = 5,
        max_results: int = 10,
    ) -> List[Dict[str, Any]] | None:
        """Get messages by filter"""
        # configure endpoint url
        if forder_id is None:
            endpoint = f"{MS_GRAPH_ME_MSGS_EP}"
        else:
            endpoint = f"{MS_GRAPH_ME_FOLDERS_EP}/{forder_id}/messages"

        headers = self.get_auth_headers()

        # filter and search parameters can't be used together
        params: Dict[str, Any] | None = {
            "$filter": filter_str,
            "$select": fields,
            "$top": min(top, max_results),
        }

        messages: List[Dict[str, Any]] = []
        next_link = endpoint

        while next_link and len(messages) < max_results:
            response = httpx.get(next_link, headers=headers, params=params)
            if response.status_code != 200:
                raise httpx.RequestError(f"Failed to retrieve emails: {response.text}")

            json_response = response.json()
            messages.extend(json_response.get("value", []))
            next_link = json_response.get("@odata.nextLink", None)
            params = None

            if next_link and len(messages) + top > max_results:
                params = {
                    "$top": min(top, max_results - len(messages)),
                }

        return messages[:max_results]

    def get_attachments(self, message_id: str) -> List[Dict[str, Any]] | Any:
        """Get attachments for a message"""
        attachments_endpoint = f"{MS_GRAPH_ME_MSGS_EP}/{message_id}/attachments"
        response = httpx.get(attachments_endpoint, headers=self.get_auth_headers())
        response.raise_for_status()
        return response.json().get("value", [])

    def download_attachment(
        self, message_id: str, attachment_id: str, attachments_name: str, folder: str
    ) -> bool:
        """Download attachment for a message"""
        downwload_ep = (
            f"{MS_GRAPH_ME_MSGS_EP}/{message_id}/attachments/{attachment_id}/$value"
        )
        response = httpx.get(downwload_ep, headers=self.get_auth_headers())
        response.raise_for_status()
        file = Path(folder) / attachments_name
        file.write_bytes(response.content)
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
