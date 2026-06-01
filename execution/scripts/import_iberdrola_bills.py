#!/usr/bin/env python3
"""
Import Iberdrola utility bills from PDF files and save to parquet via MCP.

Extracts data from Iberdrola PDF bills and creates:
- Contact record for Iberdrola
- Flow entries for each bill
- Transaction entries (if applicable)
"""

import re
import sys
from pathlib import Path
from typing import Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.extract_pdf_text import extract_pdf_text


def parse_spanish_date(date_str: str) -> Optional[str]:
    """Parse Spanish date string to YYYY-MM-DD format."""
    month_map = {
        "enero": "01",
        "febrero": "02",
        "marzo": "03",
        "abril": "04",
        "mayo": "05",
        "junio": "06",
        "julio": "07",
        "agosto": "08",
        "septiembre": "09",
        "octubre": "10",
        "noviembre": "11",
        "diciembre": "12",
        "ene": "01",
        "feb": "02",
        "mar": "03",
        "abr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "ago": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dic": "12",
    }

    # Try different date patterns
    patterns = [
        r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",  # "14 de enero de 2026"
        r"(\d{1,2})/(\d{1,2})/(\d{4})",  # "10/12/2025"
        r"(\d{4})-(\d{2})-(\d{2})",  # "2026-01-14"
    ]

    for pattern in patterns:
        match = re.search(pattern, date_str.lower())
        if match:
            if len(match.groups()) == 3:
                g1, g2, g3 = match.groups()
                # Check if it's Spanish format (day de month de year)
                if "de" in date_str.lower() and g2 in month_map:
                    day, month_name, year = g1, g2, g3
                    month = month_map[month_name.lower()]
                    return f"{year}-{month}-{day.zfill(2)}"
                # Check if it's DD/MM/YYYY
                elif "/" in date_str:
                    day, month, year = g1, g2, g3
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                # Check if it's YYYY-MM-DD
                elif len(g1) == 4:
                    return f"{g1}-{g2}-{g3}"

    return None


def extract_amount(text: str) -> Optional[float]:
    """Extract amount in EUR from text."""
    # Look for patterns like "126,45 €" or "126.45 €" or "TOTAL 126,45 €"
    patterns = [
        r"total[:\s]+([\d.,]+)\s*€",
        r"importe[:\s]+([\d.,]+)\s*€",
        r"([\d.,]+)\s*€",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Take the largest amount (usually the total)
            amounts = []
            for match in matches:
                # Replace comma with dot for decimal
                amount_str = match.replace(".", "").replace(",", ".")
                try:
                    amounts.append(float(amount_str))
                except ValueError:
                    continue
            if amounts:
                return max(amounts)

    return None


def extract_invoice_number(text: str) -> Optional[str]:
    """Extract invoice number from text."""
    patterns = [
        r"n[úu]mero\s+de\s+factura[:\s|]+([\d]+)",
        r"n[úu]\s+factura[:\s]+([\d]+)",
        r"factura[:\s]+([\d]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def extract_consumption(text: str) -> Optional[float]:
    """Extract consumption in kWh from text.

    Looks for the total consumption value which appears after "Consumo total de esta factura"
    in the format: "XXX kWh Y,YY € Z,ZZ €"
    """
    # Pattern 1: Look for consumption after "Consumo total de esta factura"
    # Format: "Consumo total de esta factura... XXX kWh Y,YY €"
    pattern1 = r"consumo\s+total\s+de\s+esta\s+factura[^\d]*?(\d+(?:[.,]\d+)?)\s*kwh"
    match = re.search(pattern1, text, re.IGNORECASE | re.DOTALL)
    if match:
        consumption_str = match.group(1).replace(",", ".")
        try:
            return float(consumption_str)
        except ValueError:
            pass

    # Pattern 2: Look for "Energía consumida" total (sum of partial consumptions)
    # This appears in detailed bills: "Energía consumida (date-date) XXX kWh"
    energy_matches = re.findall(
        r"energ[íi]a\s+consumida[^:]*?(\d+(?:[.,]\d+)?)\s*kwh", text, re.IGNORECASE
    )
    if energy_matches:
        # Sum all partial consumptions
        total = 0.0
        for match_str in energy_matches:
            try:
                total += float(match_str.replace(",", "."))
            except ValueError:
                continue
        if total > 0:
            return total

    # Pattern 3: Fallback - look for consumption in summary section
    # Format: "l Consumo | XXX kWh" or "Consumo: XXX kWh"
    pattern3 = r"(?:consumo|l\s+consumo)[:\s|]+\s*(\d+(?:[.,]\d+)?)\s*kwh"
    match = re.search(pattern3, text, re.IGNORECASE)
    if match:
        consumption_str = match.group(1).replace(",", ".")
        try:
            return float(consumption_str)
        except ValueError:
            pass

    return None


def extract_billing_period(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract billing period start and end dates."""
    patterns = [
        r"periodo\s+de\s+facturaci[óo]n[:\s]+(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})",
        r"del\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\s+al\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
        r"desde\s+(\d{1,2})(\d{2})(\d{4})\s+hasta\s+(\d{1,2})(\d{2})(\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            groups = match.groups()
            if len(groups) == 6:
                # Spanish format: "del 11 de Diciembre de 2025 al 11 de Enero de 2026"
                month_map = {
                    "enero": "01",
                    "febrero": "02",
                    "marzo": "03",
                    "abril": "04",
                    "mayo": "05",
                    "junio": "06",
                    "julio": "07",
                    "agosto": "08",
                    "septiembre": "09",
                    "octubre": "10",
                    "noviembre": "11",
                    "diciembre": "12",
                }
                day1, month1_name, year1, day2, month2_name, year2 = groups
                month1 = month_map.get(month1_name.lower(), "01")
                month2 = month_map.get(month2_name.lower(), "01")
                start = f"{year1}-{month1}-{day1.zfill(2)}"
                end = f"{year2}-{month2}-{day2.zfill(2)}"
                return start, end
            elif len(groups) == 4:
                # DD/MM/YYYY format
                day1, month1, year1, day2, month2, year2 = groups[:2] + groups[2:4]
                start = f"{year1}-{month1.zfill(2)}-{day1.zfill(2)}"
                end = f"{year2}-{month2.zfill(2)}-{day2.zfill(2)}"
                return start, end

    return None, None


def parse_iberdrola_bill(pdf_path: Path) -> Optional[dict]:
    """Parse an Iberdrola bill PDF and extract key information."""
    try:
        text = extract_pdf_text(pdf_path)
    except Exception as e:
        print(f"Error extracting text from {pdf_path.name}: {e}")
        return None

    # Extract invoice number
    invoice_number = extract_invoice_number(text)

    # Extract invoice date
    invoice_date = None
    date_patterns = [
        r"emitida\s+el\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
        r"fecha\s+de\s+emisi[óo]n[:\s]+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_date = parse_spanish_date(match.group(0))
            break

    # Extract billing period
    period_start, period_end = extract_billing_period(text)

    # Extract amount
    amount = extract_amount(text)

    # Extract consumption
    consumption = extract_consumption(text)

    # Extract contract number
    contract_match = re.search(r"contrato[:\s]+(\d+)", text, re.IGNORECASE)
    contract_number = contract_match.group(1) if contract_match else None

    if not invoice_number and not amount:
        print(f"Warning: Could not extract key data from {pdf_path.name}")
        return None

    return {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "period_start": period_start,
        "period_end": period_end,
        "amount_eur": amount,
        "consumption_kwh": consumption,
        "contract_number": contract_number,
        "file_path": str(pdf_path),
        "filename": pdf_path.name,
    }


def main():
    """Main function to process all Iberdrola bills."""
    import_dir = Path("/Users/markmhendrickson/Documents/data/imports/iberdrola")

    if not import_dir.exists():
        print(f"Error: Import directory not found: {import_dir}")
        return

    pdf_files = sorted(import_dir.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {import_dir}")
        return

    print(f"Found {len(pdf_files)} PDF files to process\n")

    bills = []
    for pdf_file in pdf_files:
        print(f"Processing: {pdf_file.name}")
        bill_data = parse_iberdrola_bill(pdf_file)
        if bill_data:
            bills.append(bill_data)
            print(
                f"  ✓ Invoice: {bill_data.get('invoice_number', 'N/A')}, "
                f"Amount: {bill_data.get('amount_eur', 'N/A')} €, "
                f"Date: {bill_data.get('invoice_date', 'N/A')}"
            )
        else:
            print("  ✗ Failed to parse")

    print(f"\n\nParsed {len(bills)} bills successfully")
    print("\nBill summary:")
    for bill in bills:
        print(
            f"  - Invoice {bill.get('invoice_number', 'N/A')}: "
            f"{bill.get('amount_eur', 'N/A')} €, "
            f"{bill.get('period_start', 'N/A')} to {bill.get('period_end', 'N/A')}"
        )

    # Now save via MCP - this will be done in the next step
    print("\n\nReady to save via parquet MCP. Run the MCP commands to create records.")


if __name__ == "__main__":
    main()
