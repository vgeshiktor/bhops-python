"""Extract data from a salary slip PDF file"""

import json
import os
import pprint
from typing import Any, Dict, List

import PyPDF2

# Path to the PDF file
# pdf_path = (f"/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python"
#             f"-projects/bhops/workers/yaarit.fridman/salary/yaarit-fridman"
#             f"-36155331-10-2024.pdf")


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from a PDF file"""
    # Open the PDF file in read-binary mode
    with open(pdf_path, "rb") as file:
        reader = PyPDF2.PdfReader(file)
        text = "".join(page.extract_text() for page in reader.pages)
    return text


def parse_text_to_json(text: str) -> Dict[str, Any]:
    """Extract meaningful sections from the text"""
    lines = text.split("\n")

    data = {
        "personal_details": {
            "employee_number": lines[16].split(":")[0].strip(),
            "id_number": lines[15].split(":")[0].strip(),
            "name": lines[34].strip(),
            "street_address": lines[35],
            "city": lines[36],
        }
    }

    # Salary details
    data["salary_details"] = {
        "base_salary": lines[60].strip(),
        "travel_expenses": lines[67].strip(),
        # "bonus": lines[31].split()[1],
        "gross_salary": lines[62].strip(),
        "total_salary": lines[86].strip(),
        "net_salary": lines[88].strip(),
    }

    # Deductions
    data["deductions"] = {
        "health_tax": lines[41].strip(),
        "social_security": lines[39].strip(),
        "pension": lines[45].strip(),
        "total_deductions": lines[49].strip(),
    }

    # Other details
    data["additional_details"] = {
        "vacation_balance": lines[160].strip(),
        "sick_leave_balance": lines[152].strip(),
    }

    return data


def parse_text_to_json2(text: str) -> Dict[str, Any]:
    """Extract meaningful sections from the text"""
    lines = text.split("\n")

    data = {
        "personal_details": {
            "employee_number": "",
            "id_number": "",
            "name": "",
            "street_address": "",
            "city": "",
            "seniority": "",
            "start_date": "",
            "marital_status": "",
        },
        "salary_details": {
            "salary_month": "",
            "print_date": "",
            "base_salary": "",
            "travel_expenses": "",
            "bonus": "",
            "gross_salary": "",
            "total_salary": "",
            "net_salary": "",
        },
        "deductions": {
            "health_tax": "",
            "social_security": "",
            "pension": "",
            "total_deductions": "",
        },
        "additional_details": {"vacation_balance": "", "sick_leave_balance": ""},
    }

    for index, line in enumerate(lines):
        _extracted_from_parse_text_to_json2_36(line, data, lines, index)
    return data


# TODO Rename this here and in `parse_text_to_json2`
def _extracted_from_parse_text_to_json2_36(
    line: str,
    data: Dict[str, Any],
    lines: List[str],
    index: int,
) -> None:
    print(line)
    if "תלוש שכר לחודש" in line:
        data["salary_details"]["salary_month"] = line.split()[0].strip()
    if "הודפס בתאריך" in line:
        data["salary_details"]["print_date"] = line.split()[0].strip()
    if "מספר זהות" in line:
        data["personal_details"]["id_number"] = line.split(":")[0].strip()
    if "מספר העובד" in line:
        data["personal_details"]["employee_number"] = line.split(":")[0].strip()
    if "וותק" in line:
        data["personal_details"]["seniority"] = line.split(":")[0].strip()
    if "תחילת עבודה" in line:
        data["personal_details"]["start_date"] = line.split(":")[0].strip()
    if "תחילת עבודה" in line:
        data["personal_details"]["marital_status"] = line.split(":")[0].strip()
    if "לכבוד" in line:
        data["personal_details"]["name"] = lines[index + 1]
        data["personal_details"]["street_address"] = lines[index + 2]
        data["personal_details"]["city"] = lines[index + 3]


def main() -> None:
    """Main function"""

    # print current worker directory
    print(os.getcwd())
    pdf_path = "yaarit-fridman-36155331-10-2024.pdf"
    pdf_path = "salaryops/tamara-alexandrov-320721582-10-2024.pdf"
    pdf_path = "salaryops/alisheva-malka-302898507-10-2024.pdf"
    print(pdf_path)

    # Extract and parse the text
    pdf_text = extract_text_from_pdf(pdf_path)
    parsed_data = parse_text_to_json2(pdf_text)

    # # Convert parsed data to JSON
    json_output = json.dumps(parsed_data, indent=4, ensure_ascii=False)
    pprint.pprint(parsed_data)

    # Save the JSON output to a file
    output_path = (
        "/Users/vadimgeshiktor/repos/github.com/vgeshiktor/python"
        "-projects/bhops/workers/yaarit.fridman/salary/yaarit-fridman"
        "-36155331-10-2024.json"
    )
    with open(output_path, "w", encoding="utf-8") as json_file:
        json_file.write(json_output)

    print(f"JSON data saved to {output_path}")


if __name__ == "__main__":
    main()
