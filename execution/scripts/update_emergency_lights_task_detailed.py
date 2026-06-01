#!/usr/bin/env python3
"""
Update task for emergency flood lights check for David with comprehensive details.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

tasks_file = Path("data/tasks/tasks.parquet")
if not tasks_file.exists():
    print(f"Tasks file not found: {tasks_file}")
    sys.exit(1)

# Create snapshot before modification
snapshot_dir = Path("data/snapshots")
snapshot_dir.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
df_original = pd.read_parquet(tasks_file)
snapshot_path = snapshot_dir / f"tasks-{timestamp}.parquet"
df_original.to_parquet(snapshot_path, index=False)
print(f"Created snapshot: {snapshot_path}")

# Read tasks
df = pd.read_parquet(tasks_file)

# Find the specific task
task_id = "1212567742771352"
task_mask = df["task_id"] == task_id

if not task_mask.any():
    print(f"Task {task_id} not found")
    sys.exit(1)

task_idx = df[task_mask].index[0]
print(f"Found task: {df.loc[task_idx, 'title']}")
print(f"Current status: {df.loc[task_idx, 'status']}")

# Update task with comprehensive details
df.loc[
    task_idx, "title"
] = "Check potency of emergency flood lights for David - Back Façade"
df.loc[
    task_idx, "description"
] = "Verify potency (illumination levels) of two emergency flood lights on back façade (fachada trasera) of [See properties.parquet - Barcelona property address]. These are exterior-mounted emergency flood lights, different from the stairwell emergency lights. Need to check battery backup functionality, proper illumination levels, weather resistance, compliance with safety regulations, operational status, and obtain model numbers for replacement parts if needed."
df.loc[task_idx, "domain"] = "admin"
df.loc[task_idx, "status"] = "in_progress"

# Comprehensive notes
comprehensive_notes = """**Task Context:**
Check potency (illumination levels) of two emergency flood lights on back façade (fachada trasera) of [See properties.parquet - Barcelona property address]. These are exterior-mounted emergency flood lights, different from the stairwell emergency lights.

**Device Details:**
- Location: Back façade (fachada trasera)
- Quantity: 2 units
- Type: Emergency flood lights (exterior)
- Purpose: Emergency illumination for back façade only
- Installation Period: 2021 (during construction)
- Different from: Stairwell emergency lights (which are interior, in staircase)

**What Needs to be Checked:**
- Battery backup functionality
- Proper illumination levels (potency/luminosity)
- Weather resistance and exterior mounting integrity
- Compliance with safety regulations
- Operational status
- Model number and manufacturer for replacement parts if needed

**Dec 23, 2025 - Documentation Search Completed:**

**Gmail Archive Search:**
- Searched all emails related to Legrand/Netatmo installation in Barcelona
- Reviewed emails with Ana Bragatti (anabragatti@coac.net) and Ricard Fayos (ricardfayos@coac.net)
- Searched pre-2021 emails (2019-2020) and post-2021 emails (2021-2024)

**Certification PDFs Reviewed:**
- XEDEX Cert 20 Partidas (Dec 2021) - 704 KB - No back façade emergency flood light specs found
- XEDEX Cert 19 Partidas (Nov 2021) - 691 KB - No back façade emergency flood light specs found
- XEDEX Cert 18 Partidas (Nov 2021) - 671 KB - No back façade emergency flood light specs found
- XEDEX Cert 17 Partidas (Oct 2021) - 661 KB - No back façade emergency flood light specs found

**What Was Found in PDFs:**
- General lighting installation (Chapter 27.02) mentions "luminarias led de pared para exteriores" (exterior wall LED lights) but no specific model numbers
- Elevator emergency light mentioned - this is for the elevator, not the back façade
- No specific line items for "emergency flood lights" or "proyector emergencia" on back façade
- No items with quantity 2 that match emergency/exterior flood light descriptions

**Email Search Results:**

**Pre-2021 Emails:**
- 2019: XEDEX budget documents (Nov 2019) - Original project budget, no back façade emergency flood lights found
- 2020: Electrical/lighting installation emails (Oct-Nov 2020):
  - "RELATIVO A LAS INSTALACIONES ELECTRICAS-ILUMINACION" (Oct 30, 2020) - Contains lighting list PDF
  - "instalaciones" (Nov 24, 2020) - Netatmo device specifications
  - Lighting list PDF: Only regular exterior wall lights (APLIQUES DE PARED EXTERIOR), no emergency flood lights
- 2020: Safety/security emails (Dec 2020) - Work safety requirements, no emergency lighting specifications

**Post-2021 Emails:**
- Oct 2023: "Lista de lampistería para Alió, 18" mentions "Arreglar el parpadeo de dos luces de emergencia en la escalera" (fix flickering of two emergency lights in stairwell) - **Note:** This refers to stairwell lights, NOT the back façade flood lights
- GRUPO KIAK Budget PR-23-227 (Nov 2023): Contains reference to stairwell emergency lights but no mention of back façade emergency flood lights
- Jan 2024: Javier Hita (GRUPO KIAK) requested lighting specifications for dimmer installation, but only for interior lights (salón and comedor)

**Conclusion:**
The back façade emergency flood lights are NOT documented in any pre-2021 or post-2021 emails or project documents. They may have been:
- Added during construction as a building code/safety requirement (not explicitly documented)
- Installed as a modification order not captured in the emails reviewed
- Required by building regulations but not detailed in project specifications

**Action Taken - Dec 23, 2025:**
- Email sent to Ana Bragatti (CC: Ricard Fayos) requesting:
  1. Documentation for back-façade emergency flood lights (model numbers, specifications)
  2. General documentation about installed lights and home automation (Netatmo/Legrand) for records
- Email Message ID: `19b4b9d620ef9407`
- Summary document created/updated: `operations/admin/legrand-netatmo-installation-barcelona-summary.md`

**Alternative Sources for Model Numbers:**
1. PC 28 documents (Mecanismos Inteligentes) - may contain device specifications
2. GRUPO KIAK invoices/budgets - may have detailed device lists (checked PR-23-227, no back façade flood lights found)
3. Contact XEDEX S.A. or GRUPO KIAK directly for device specifications
4. Check physical installation - model numbers may be visible on the devices themselves
5. Review original construction project documents from 2019-2021 (XEDEX budget documents)
6. Check if back façade emergency flood lights were part of original project or added later

**Next Steps:**
1. **Await response from Ana Bragatti** - Email sent Dec 23, 2025 requesting device specifications
2. Verify current operational status of both emergency lights
3. Test battery backup functionality
4. Measure illumination levels (potency)
5. Check for any visible damage or wear
6. Check physical installation for visible model numbers
7. Document findings for David
8. Determine if repair or replacement is needed

**Related Documents:**
- Summary: `operations/admin/legrand-netatmo-installation-barcelona-summary.md`
- Email draft: `operations/admin/email-ana-bragatti-device-specifications-draft.md`
- Extraction instructions: `operations/admin/emergency-flood-lights-extraction-instructions.md`

**Related Email Threads:**
- "Consulta sobre especificaciones de dispositivos Netatmo/Legrand" (Dec 23, 2025) - Request to Ana Bragatti
- "Lista de lampistería para Alió, 18" (Oct 2023 - Jan 2024)
- "PC 28 mecanismos inteligentes" (Nov-Dec 2021)
- Netatmo system access confirmations (2021-2022)

**Contractor Contacts:**
- Ana Bragatti: anabragatti@coac.net (Architect/Project Manager)
- Ricard Fayos: ricardfayos@coac.net (Architect)
- XEDEX S.A.: administracion@xedexsa.com (Constructor)
- GRUPO KIAK: javier.hita@grupokiak.com (Electrical contractor)
"""

df.loc[task_idx, "notes"] = comprehensive_notes

# Update timestamp
if df["updated_at"].dtype.tz is not None:
    df.loc[task_idx, "updated_at"] = pd.Timestamp.now(tz=df["updated_at"].dtype.tz)
else:
    df.loc[task_idx, "updated_at"] = pd.Timestamp.now()

# Write updated tasks
df.to_parquet(tasks_file, index=False)
print(f"\nUpdated task {task_id} with comprehensive details")
print(f"Updated tasks file: {tasks_file}")
