"""
    This module is the entry point of the application.
    It downloads salary PDFs from emails, distributes them to employees,
    and sends a notification email to the employees.
"""

import json
import locale
import os
from mimetypes import guess_extension
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from salaryops.emailmanager import EmailManager
from salaryops.pdfmanager import PDFManager

email_manager: Optional[EmailManager]
pdf_manager: Optional[PDFManager]
config: Dict[str, Any] = {}


def main() -> None:
    """
    Main function to download salary PDFs, distribute them,
    and send notification emails
    """

    global config, email_manager, pdf_manager

    # Load environment variables from .env file
    load_dotenv()
    app_id: str = os.getenv("APP_ID", "")
    secret: str = os.getenv("SECRET", "")
    scopes: List[str] = ["User.Read", "Mail.ReadWrite", "Mail.Send"]

    config = init_config()

    init_email_manager(app_id, secret, scopes)

    init_pdf_manager()

    download_salary_pdfs()

    distribute_salary_pdfs()

    publish_salary_pdfs()


def init_config() -> Any:
    """Initialize configuration"""
    # set locale
    locale.setlocale(locale.LC_ALL, "he_IL")

    config_prefix = os.getenv("BHOPS_CONFIG_PREFIX", ".")
    config_file = os.getenv("BHOPS_CONFIG_FILE", "bhops.settings.json")

    # Read configuration from bhops.settings.json
    config_path = Path(config_prefix) / config_file
    with config_path.expanduser().open(encoding="utf8") as f:
        cfg = json.load(f)

    return cfg


def download_salary_pdfs() -> None:
    """Download salary PDFs from emails"""
    global config, email_manager

    # Create the folder to store the downloaded salary PDFs
    base_folder = config["salaryops"]["base_folder"]
    downloads = config["salaryops"]["slips_downloads_folder"]
    downloads_folder = Path(base_folder).expanduser() / downloads
    downloads_folder.mkdir(parents=True, exist_ok=True)

    # clean downloads folder
    for file in downloads_folder.glob("*"):
        file.unlink()

    # create query filter
    sal_filter = (
        r"from/emailAddress/address eq 'yael@damsalem.co.il' and "
        r"contains(subject, 'שכר') and "
        r"hasAttachments eq true and "
        r"receivedDateTime ge 2024-11-01T00:00:00Z",
    )

    messages = email_manager.get_messages_by_filter(sal_filter)  # type: ignore

    for message in messages:  # type: ignore
        attachments = email_manager.get_attachments(message["id"])  # type: ignore
        for attachment in attachments:
            attachment_extension = guess_extension(
                attachment["contentType"], strict=True
            )
            attachment_name = (
                f'sal-{attachment["lastModifiedDateTime"]}{attachment_extension}'
            )
            attachment_name = attachment_name.replace(":", "-")
            email_manager.download_attachment(  # type: ignore
                message["id"], attachment["id"], attachment_name, str(downloads_folder)
            )


def distribute_salary_pdfs() -> None:
    """Distribute salary PDFs to employees"""

    global config, pdf_manager

    # Create the folder for workers
    base_folder: str = config["salaryops"]["base_folder"]
    workers_folder: Path = (
        Path(base_folder).expanduser() / config["salaryops"]["workers_folder"]
    )
    workers_folder.mkdir(parents=True, exist_ok=True)

    pdf_manager.distribute_pdfs()  # type: ignore


def publish_salary_pdfs() -> None:
    """Publish salary PDFs using email"""

    # Create the folder for workers
    base_folder = config["salaryops"]["base_folder"]
    workers = config["salaryops"]["workers_folder"]
    folder = Path(base_folder).expanduser() / workers
    folder.mkdir(parents=True, exist_ok=True)

    email_manager.publish_salary_pdfs()  # type: ignore


def init_email_manager(app_id: str, secret: str, scopes: List[str]) -> None:
    """Initialize the EmailManager"""
    global email_manager
    manager = EmailManager(config, app_id=app_id, secret=secret, scopes=scopes)
    manager.login()
    email_manager = manager


def init_pdf_manager() -> None:
    """Initialize the PDFManager"""
    global pdf_manager
    pdf_manager = PDFManager(config)


if __name__ == "__main__":
    main()
