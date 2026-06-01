#!/usr/bin/env python3
"""
Build a Neotoma ICP priority list from LinkedIn Connections export.
Reads: Connections.csv, Invitations.csv, Endorsement_Received_Info.csv
Outputs: Markdown list grouped by ICP tier (per docs/specs/ICP_PRIORITY_TIERS.md).
Warm = in Invitations (incoming) or Endorsement_Received; listed first within each tier.
"""
import csv
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

# LinkedIn export folder (path from user; linkedin.zip is a directory)
LINKEDIN_DIR = Path("/Users/markmhendrickson/Documents/data/imports/linkedin.zip")
# Output: ateles repo; copy to neotoma/docs/specs/ if desired
OUT_PATH = (
    Path(__file__).resolve().parents[2] / "docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md"
)

# Keywords per tier (position + company). First match wins; Tier 1 checked first.
TIER1_POSITION = re.compile(
    r"\b(Founder|Co-Founder|CTO|CEO\b|Chief\s+Product|Product\s+Lead|Head\s+of\s+Product|"
    r"Product\s+Manager|PM\b|Technical\s+Product|Analyst|Researcher|Consultant|"
    r"Attorney|Counsel|Lawyer|DevRel|Developer\s+Relations|AI\s+Technical|"
    r"Knowledge\s+Worker|Chief\s+of\s+Staff|Fractional\s+CMO)\b",
    re.I,
)
TIER1_COMPANY = re.compile(
    r"\b(AI\b|Safebots|Wordsmith\s+AI|TurinTech|Agent\.ai|ReflectionAI)\b", re.I
)

TIER2_POSITION = re.compile(
    r"\b(Engineer|Developer|Software\s+Engineer|Staff\s+Engineer|SRE|"
    r"Product\s+Designer|Head\s+of\s+Design|VP\s+Product\s+Design|Design\s+Manager|"
    r"Marketing\s+Operations|RevOps|Ops\s+Manager|GTM\s+Lead|"
    r"Developer\s+Platform|API\s+|Integrator|Technical\s+Writer|Documentation)\b",
    re.I,
)
TIER2_COMPANY = re.compile(
    r"\b(Attio|Pydantic|Celonis|Stacks\s+Labs|Hiro|Trust\s+Machines)\b", re.I
)

TIER3_POSITION = re.compile(
    r"\b(Freelance|Solopreneur|Self-employed|Independent\s+Consultant)\b", re.I
)

TIER4_POSITION = re.compile(
    r"\b(General\s+Partner|Partner\b|GP\b|Investor|Angel|VC\b|Venture\s+Capital|"
    r"Family\s+Office|HNW|Wealth)\b",
    re.I,
)
TIER4_COMPANY = re.compile(
    r"\b(Asymmetric|Antler|Pear\s+VC|Antigravity|Village\s+Global|Y\s+Combinator)\b",
    re.I,
)

TIER5_POSITION = re.compile(
    r"\b(Blockchain|Crypto|Bitcoin|Stacks|Protocol|On-chain|Wallet)\b", re.I
)
TIER5_COMPANY = re.compile(
    r"\b(Trust\s+Machines|Hiro|Stacks|Stacks\s+Labs|Kraken|Galaxy|Coinbase|"
    r"Ethereum\s+Foundation|Bitcoin|Velar|Chorus\s+One|Rootstock|Mercuryo|"
    r"Alchemy|OKX|Velar|Joltz|Ryder|block9|1tao)\b",
    re.I,
)

TIER6_POSITION = re.compile(
    r"\b(Director|VP\s+|Head\s+of\s+)(AI|Agent|Machine\s+Learning|ML)\b", re.I
)


def normalize_url(url: str) -> str:
    if not url or not url.strip():
        return ""
    u = url.strip()
    if u.startswith("www."):
        u = "https://" + u
    elif not u.startswith("http"):
        u = "https://" + u
    # Normalize to https://www.linkedin.com/in/XXX
    parsed = urlparse(u)
    path = (parsed.path or "").rstrip("/")
    if path and not path.startswith("/in/"):
        path = "/in/" + path.lstrip("/")
    return f"https://www.linkedin.com{path}" if path else ""


def extract_slug(url: str) -> str:
    p = urlparse(url if url.startswith("http") else "https://" + url)
    path = (p.path or "").strip("/")
    return path.lower() if path else ""


def classify_tier(position: str, company: str) -> int:
    pos = position or ""
    comp = company or ""
    combined = f"{pos} {comp}"

    if TIER6_POSITION.search(combined):
        return 6
    if TIER5_COMPANY.search(comp) or TIER5_POSITION.search(combined):
        return 5
    if TIER4_COMPANY.search(comp) or TIER4_POSITION.search(combined):
        return 4
    if TIER3_POSITION.search(combined):
        return 3
    if TIER2_COMPANY.search(comp) or TIER2_POSITION.search(combined):
        return 2
    if TIER1_COMPANY.search(comp) or TIER1_POSITION.search(combined):
        return 1
    return 0  # unclassified


def load_connections(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        # Skip Notes lines until we see the real header
        header_line = None
        for line in f:
            if "First Name" in line and "Last Name" in line and "URL" in line:
                header_line = line
                break
        if not header_line:
            return rows
        reader = csv.DictReader(
            f, fieldnames=[x.strip() for x in header_line.split(",")]
        )
        for row in reader:
            first = (row.get("First Name") or "").strip()
            url = (row.get("URL") or "").strip()
            if not url or "linkedin.com" not in url:
                continue
            if not first and not (row.get("Last Name") or "").strip():
                continue
            rows.append(
                {
                    "First Name": first,
                    "Last Name": (row.get("Last Name") or "").strip(),
                    "URL": url,
                    "Email": (row.get("Email Address") or "").strip(),
                    "Company": (row.get("Company") or "").strip(),
                    "Position": (row.get("Position") or "").strip(),
                    "Connected On": (row.get("Connected On") or "").strip(),
                }
            )
    return rows


def load_warm_urls(linkedin_dir: Path) -> set[str]:
    slugs = set()
    # Invitations: inviterProfileUrl
    inv_path = linkedin_dir / "Invitations.csv"
    if inv_path.exists():
        with open(inv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("inviterProfileUrl") or "").strip()
                if url:
                    slugs.add(extract_slug(normalize_url(url)))
    # Endorsement_Received_Info: Endorser Public Url
    end_path = linkedin_dir / "Endorsement_Received_Info.csv"
    if end_path.exists():
        with open(end_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("Endorser Public Url") or "").strip()
                if url:
                    slugs.add(extract_slug(normalize_url(url)))
    return slugs


def main() -> None:
    linkedin_dir = LINKEDIN_DIR
    conn_path = linkedin_dir / "Connections.csv"
    if not conn_path.exists():
        raise SystemExit(f"Connections not found: {conn_path}")

    connections = load_connections(conn_path)
    warm_slugs = load_warm_urls(linkedin_dir)

    by_tier = defaultdict(list)
    unclassified = []

    for c in connections:
        url = c["URL"]
        slug = extract_slug(normalize_url(url))
        tier = classify_tier(c["Position"], c["Company"])
        c["_slug"] = slug
        c["_warm"] = slug in warm_slugs
        if tier:
            by_tier[tier].append(c)
        else:
            unclassified.append(c)

    # Sort within tier: warm first, then by date (recent first - parse "11 Feb 2026" style)
    def sort_key(c):
        try:
            from datetime import datetime

            d = c.get("Connected On") or ""
            if d:
                dt = datetime.strptime(d.strip(), "%d %b %Y")
                date_ord = dt.toordinal()
            else:
                date_ord = 0
        except Exception:
            date_ord = 0
        return (0 if c["_warm"] else 1, -date_ord)

    for tier in by_tier:
        by_tier[tier].sort(key=sort_key)

    # Write markdown
    tier_labels = {
        1: "Tier 1 — MVP target (AI-Native Individuals, Knowledge Workers, Small Teams)",
        2: "Tier 2 — Early B2B (Product Teams, Ops, Developer/AI Integrators)",
        3: "Tier 3 — B2C Power Users (Solopreneurs, Multi-System, Households)",
        4: "Tier 4 — Strategy layer (HNW, Crypto, Founders/Equity)",
        5: "Tier 5 — Execution layer (Bitcoin/Stacks, On-chain, Protocol)",
        6: "Tier 6 — Enterprise AI deployments",
    }

    lines = [
        "# LinkedIn contacts by Neotoma ICP priority",
        "",
        "Source: LinkedIn data export (Connections, Invitations, Endorsements).",
        "ICP tiers from [ICP_PRIORITY_TIERS.md](./ICP_PRIORITY_TIERS.md).",
        "",
        "**Warm** = reached out to you (Invitations) or endorsed you (Endorsement_Received).",
        "Within each tier, warm contacts are listed first, then by connection date (newest first).",
        "",
        "---",
        "",
    ]

    for tier in sorted(by_tier.keys()):
        label = tier_labels.get(tier, f"Tier {tier}")
        lines.append(f"## {label}")
        lines.append("")
        for c in by_tier[tier]:
            warm_mark = " **[WARM]**" if c["_warm"] else ""
            name = f"{c['First Name']} {c['Last Name']}".strip()
            pos = c["Position"] or "—"
            company = c["Company"] or "—"
            url = c["URL"]
            date = c["Connected On"] or ""
            lines.append(
                f"- {name}{warm_mark} — {pos} @ {company} | [Profile]({url}) | {date}"
            )
        lines.append("")

    lines.append("## Unclassified (no ICP tier match)")
    lines.append("")
    for c in sorted(unclassified, key=sort_key)[:80]:  # cap for length
        warm_mark = " **[WARM]**" if c["_warm"] else ""
        name = f"{c['First Name']} {c['Last Name']}".strip()
        pos = c["Position"] or "—"
        company = c["Company"] or "—"
        url = c["URL"]
        lines.append(f"- {name}{warm_mark} — {pos} @ {company} | [Profile]({url})")
    if len(unclassified) > 80:
        lines.append(f"- … and {len(unclassified) - 80} more")
    lines.append("")

    out = OUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(
        f"Wrote {out} ({sum(len(by_tier[t]) for t in by_tier)} tiered, {len(unclassified)} unclassified)"
    )


if __name__ == "__main__":
    main()
