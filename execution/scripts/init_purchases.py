#!/usr/bin/env python3
"""
Initialize purchases tracking with initial items.
"""

import os
import uuid
from datetime import date

import pandas as pd

# Define the purchases directory
purchases_dir = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "purchases"
)
os.makedirs(purchases_dir, exist_ok=True)

# Initial purchases for Castellón home
initial_purchases = [
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Smart plugs for heaters",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Home Improvement",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Bath mats",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Home Essentials",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Fire starter",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Outdoor/BBQ",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Large tower for coals or similar",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Outdoor/BBQ",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Large bags of coals",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Outdoor/BBQ",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Tongs shovel etc tools for bbq",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Outdoor/BBQ",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Lighters and matches",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Outdoor/BBQ",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Folding tables and chairs",
        "status": "pending",
        "location": "Castellón",
        "priority": "medium",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Furniture",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
    {
        "purchase_id": str(uuid.uuid4()),
        "item_name": "Heater gas",
        "status": "pending",
        "location": "Castellón",
        "priority": "high",
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "currency": "EUR",
        "category": "Home Improvement",
        "notes": "",
        "created_date": date.today(),
        "completed_date": None,
        "vendor": None,
    },
]

# Create DataFrame
df = pd.DataFrame(initial_purchases)

# Save to parquet
output_file = os.path.join(purchases_dir, "purchases.parquet")
df.to_parquet(output_file, index=False)

print(f"Created purchases file with {len(initial_purchases)} items")
print(f"Output file: {output_file}")
