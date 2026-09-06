#!/usr/bin/env python3
"""Check that every intra-foundation link in docs/foundation/ resolves.

A link of the form ``[text](other.md#section)``, ``[text](#section)``, or a bare backticked citation
``other.md#section`` must name a file in ``docs/foundation/`` and, when it carries a fragment, a heading
in that file whose GitHub-style anchor equals the fragment. Broken links are printed as ``file:line``
and the check exits 1.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_CITE_RE = re.compile(r"`([\w./-]+\.md#[\w.-]+)`")


def anchor(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    text = text.replace(" ", "-")
    return text


def headings(path: Path) -> set[str]:
    out: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        a = anchor(m.group(2))
        n = seen.get(a, 0)
        seen[a] = n + 1
        out.add(a if n == 0 else f"{a}-{n}")
    return out


class MissingCorpus(Exception):
    """The directory this check inspects is absent or empty.

    Exiting 0 on a missing corpus reports a pass for a check that never ran — the "reports without
    binding" defect the foundation documents name. The check fails closed instead, naming the root it
    inspected so a wrong ``--root`` is distinguishable from a genuinely missing directory.
    """


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    if not fdir.is_dir():
        raise MissingCorpus(
            f"no {fdir} (looked under --root {root}); nothing was checked. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )
    files = sorted(fdir.glob("*.md"))
    if not files:
        raise MissingCorpus(
            f"{fdir} contains no .md files (looked under --root {root}); nothing was checked. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )
    table = {p.name: headings(p) for p in files}
    broken: list[str] = []
    for path in files:
        rel = str(path.relative_to(root))
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            targets = _LINK_RE.findall(line) + _CITE_RE.findall(line)
            for t in targets:
                if t.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, frag = t.partition("#")
                if file_part and not file_part.endswith(".md"):
                    continue
                if file_part and "/" in file_part:
                    continue  # links outside this directory are not this check's business
                name = file_part or path.name
                if name not in table:
                    broken.append(f"{rel}:{no}: missing file {name} in {t}")
                    continue
                if frag and frag not in table[name]:
                    broken.append(f"{rel}:{no}: missing anchor #{frag} in {name}")
    return broken


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    try:
        broken = check(args.root)
    except MissingCorpus as exc:
        print(f"anchor check: {exc}")
        return 1
    for b in broken:
        print(b)
    print(f"anchor check: {len(broken)} broken link(s)")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
