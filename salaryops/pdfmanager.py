"""PDF Manager module"""

import datetime
from pathlib import Path
from typing import Any, Dict

import dateutil.relativedelta
import PyPDF2


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
        text.replace("\n", "")

        # get current month and year
        now = datetime.datetime.now()
        payment_date = now + dateutil.relativedelta.relativedelta(months=-1)

        for worker_id in self._workers.keys():
            if worker_id not in text:
                continue

            # worker found, create salary file name
            worker: Dict[str, Any] = self._workers[worker_id]
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
