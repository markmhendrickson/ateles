#!/usr/bin/env python3
"""
Save parsed Iberdrola bills to parquet via MCP.

This script:
1. Parses all Iberdrola PDF bills
2. Deduplicates by invoice number
3. Creates/updates Iberdrola contact
4. Creates flow entries for each bill
"""

import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.import_iberdrola_bills import (
    parse_iberdrola_bill,
)


def get_or_create_iberdrola_contact():
    """Get or create Iberdrola contact record."""
    # Check if contact exists

    # This would use MCP in real scenario - for now return contact data
    return {
        "name": "Iberdrola Clientes S.A.U.",
        "contact_type": "business",
        "category": "utilities",
        "platform": "Iberdrola",
        "email": "clientes@tuiberdrola.es",
        "phone": "900 225 235",
        "address": "Plaza Euskadi 5, 48009 Bilbao, Spain",
        "country": "Spain",
        "website": "https://www.iberdrola.es",
        "language": "Spanish",
    }


def create_flow_entry(bill_data: dict, contact_id: str = None) -> dict:
    """Create a flow entry from bill data."""
    from datetime import date

    # Use invoice date or period end as flow date
    flow_date = (
        bill_data.get("invoice_date")
        or bill_data.get("period_end")
        or date.today().isoformat()
    )

    # Convert EUR to USD (approximate - should use actual exchange rate)
    amount_eur = bill_data.get("amount_eur", 0)
    amount_usd = amount_eur * 1.08  # Approximate EUR to USD rate

    # Extract year from date
    year = int(flow_date.split("-")[0]) if flow_date else datetime.now().year

    return {
        "flow_name": f"Iberdrola Electricity Bill - Invoice {bill_data.get('invoice_number', 'N/A')}",
        "flow_date": flow_date,
        "year": year,
        "timeline": f"{bill_data.get('period_start', 'N/A')} to {bill_data.get('period_end', 'N/A')}",
        "amount_usd": round(amount_usd, 2),
        "amount_original": round(amount_eur, 2),
        "currency_original": "EUR",
        "for_cash_flow": True,
        "party": "Iberdrola Clientes S.A.U.",
        "flow_type": "utility_bill",
        "location": "C/ TRAS EL SANTO, 11, SAN VICENTE PIEDRAHITA, 12126 CORTES DE ARENOSO (CASTELLON)",
        "category": "utilities_electricity",
        "status": "paid",
        "notes": (
            f"Invoice Number: {bill_data.get('invoice_number', 'N/A')}\n"
            f"Contract Number: {bill_data.get('contract_number', 'N/A')}\n"
            f"Consumption: {bill_data.get('consumption_kwh', 'N/A')} kWh\n"
            f"Billing Period: {bill_data.get('period_start', 'N/A')} to {bill_data.get('period_end', 'N/A')}\n"
            f"Source File: {bill_data.get('filename', 'N/A')}"
        ),
        "import_date": date.today().isoformat(),
        "import_source_file": bill_data.get("file_path", ""),
    }


def main():
    """Main function to process and save all Iberdrola bills."""
    import_dir = Path("/Users/markmhendrickson/Documents/data/imports/iberdrola")

    if not import_dir.exists():
        print(f"Error: Import directory not found: {import_dir}")
        return

    pdf_files = sorted(import_dir.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {import_dir}")
        return

    print(f"Processing {len(pdf_files)} PDF files...\n")

    # Parse all bills
    all_bills = []
    for pdf_file in pdf_files:
        bill_data = parse_iberdrola_bill(pdf_file)
        if bill_data:
            all_bills.append(bill_data)

    # Deduplicate by invoice number (keep the one with more complete data)
    unique_bills = {}
    for bill in all_bills:
        invoice_num = bill.get("invoice_number")
        if invoice_num:
            if invoice_num not in unique_bills:
                unique_bills[invoice_num] = bill
            else:
                # Keep the one with more complete data
                existing = unique_bills[invoice_num]
                if (bill.get("amount_eur") and not existing.get("amount_eur")) or (
                    bill.get("consumption_kwh") and not existing.get("consumption_kwh")
                ):
                    unique_bills[invoice_num] = bill

    bills = list(unique_bills.values())
    print(f"Found {len(bills)} unique bills after deduplication\n")

    # Print summary
    print("Bills to import:")
    for bill in sorted(bills, key=lambda x: x.get("invoice_date", "")):
        print(
            f"  - Invoice {bill.get('invoice_number', 'N/A')}: "
            f"{bill.get('amount_eur', 'N/A')} €, "
            f"Date: {bill.get('invoice_date', 'N/A')}, "
            f"Period: {bill.get('period_start', 'N/A')} to {bill.get('period_end', 'N/A')}"
        )

    print("\n\nReady to save via MCP. Use the following MCP commands:")
    print("\n1. Create/update Iberdrola contact")
    print("2. Create flow entries for each bill")

    return bills


if __name__ == "__main__":
    bills = main()
