#!/usr/bin/env python3
"""Regenerate the security-incident action-item page from live GitHub state.

Status is never hand-maintained: every issue/PR row is resolved from the GitHub
API at render time, so the page cannot drift the way a snapshot would. Only
items GitHub cannot know about (advisories held private, operator actions)
carry a manual_status from the manifest.

Usage:
  python3 render_action_items.py            # write html to stdout
  python3 render_action_items.py --out F    # write to file
"""
import json, subprocess, sys, html, os, argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

def gh_json(args):
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
        if out.returncode != 0: return None
        return json.loads(out.stdout)
    except Exception:
        return None

def resolve(item, repo):
    """Return (label, url, state, title) with state resolved live where possible."""
    k = item["kind"]
    if k in ("issue", "pr"):
        n = item["number"]
        d = gh_json(["issue", "view", str(n), "--repo", repo, "--json", "number,state,title,url"]) \
            or gh_json(["pr", "view", str(n), "--repo", repo, "--json", "number,state,title,url"])
        if not d:
            return (f"#{n}", f"https://github.com/{repo}/issues/{n}", "UNKNOWN", "(could not resolve)")
        return (f"#{n}", d["url"], d["state"], d["title"])
    # manual / ghsa: no live source.
    #
    # KNOWN GAP (ateles#516): this is the only status on the page that is typed
    # by hand, and it drifted within an hour of the page first shipping — the
    # client-instance deploy row still read "pending" after the deploy had
    # completed and been verified. That is the same generated-file-drift class
    # this renderer exists to prevent, surviving in the one corner it does not
    # cover. #516 proposes resolving operator-action rows from Neotoma task
    # entities the way issue rows resolve from GitHub, with honest per-row
    # degradation when Neotoma is unreachable. Until then: if you edit a
    # manual_status, check it against reality first.
    return (item.get("key", "—"), None, item.get("manual_status", "pending").upper(), item["title"])

STATE_STYLE = {
    "OPEN":   ("#8a6d00", "open"),
    "CLOSED": ("#1a7f37", "closed"),
    "MERGED": ("#1a7f37", "merged"),
}

def render(manifest, repo):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    open_n = closed_n = 0
    rows_by_group = []
    for g in manifest["groups"]:
        rows = []
        for it in g["items"]:
            label, url, state, title = resolve(it, repo)
            norm = state.upper()
            if norm in ("CLOSED", "MERGED"): closed_n += 1
            elif norm == "OPEN": open_n += 1
            rows.append((label, url, state, title, it.get("incident", ""), it.get("severity", "")))
        rows_by_group.append((g["name"], g.get("note", ""), rows))

    out = []
    A = out.append
    # The heading and framing are emitted here rather than hand-written onto the
    # page. They were hand-added on the first publish, which put them outside the
    # generator's reach — exactly the drift this script exists to prevent, since a
    # regeneration would silently drop them.
    A(f'<h1>{html.escape(manifest["title"])}</h1>')
    A(f'<p class="sub">{html.escape(manifest["subtitle"])}</p>')
    A(f'<p class="notice">Generated {html.escape(now)} from live GitHub state. '
      f'Issue and pull-request status is resolved at render time rather than recorded here, so this page cannot go stale; '
      f'items with no public tracker (advisories held private, operator actions) show a manually-maintained status.</p>')
    A(f'<p class="notice">{html.escape(manifest["scope_note"])}</p>')
    A(f'<p><strong>{open_n} open</strong> · {closed_n} closed or merged · {open_n+closed_n} tracked in total.</p>')
    for name, note, rows in rows_by_group:
        A(f'<h3>{html.escape(name)}</h3>')
        if note: A(f'<p>{html.escape(note)}</p>')
        A('<div class="tw"><table>')
        A('<tr><th>Item</th><th>Status</th><th>From</th><th>Tracked as</th></tr>')
        for label, url, state, title, incident, severity in rows:
            colour, word = STATE_STYLE.get(state.upper(), ("#57606a", state.lower()))
            badge = f'<span style="color:{colour};font-weight:600">{html.escape(word)}</span>'
            sev = f' <em>({html.escape(severity)})</em>' if severity else ''
            link = f'<a href="{html.escape(url)}">{html.escape(label)}</a>' if url else html.escape(label)
            A(f'<tr><td>{html.escape(title)}{sev}</td><td>{badge}</td>'
              f'<td>{html.escape(incident)}</td><td>{link}</td></tr>')
        A('</table></div>')

    # Also generated, for the same reason as the header above.
    A('<h3>How this page is generated</h3>')
    A('<p>This page is rendered by a script rather than edited by hand. If you are an agent or a person '
      'who spots something that needs to change here, change the source, not this page — a manual edit '
      'will be overwritten on the next regeneration.</p>')
    A('<p><strong>Source:</strong> <code>execution/scripts/security_action_items/</code> in the '
      '<code>ateles</code> repository — <code>render_action_items.py</code> plus <code>manifest.json</code>, '
      'with a README describing the contract in full.</p>')
    A('<ul>')
    A('<li><strong>To add, remove, or regroup an item:</strong> edit <code>manifest.json</code>. '
      'It carries membership and grouping only.</li>')
    A('<li><strong>To correct a status:</strong> you almost certainly cannot, and should not try. Issue and '
      'pull-request status is resolved from the GitHub API at render time, so the fix is to change the item '
      'upstream on GitHub; this page will follow. A status recorded here would drift.</li>')
    A('<li><strong>The one exception:</strong> items with no public tracker — advisories held private until '
      'their fix ships, and operator actions such as deploys, secret rotation, and allowlist changes — carry a '
      '<code>manual_status</code> in the manifest, because GitHub cannot answer for them. Do not add a '
      '<code>manual_status</code> to anything GitHub does track.</li>')
    A('<li><strong>To regenerate:</strong> run the script and write the result back onto this same entity. '
      'Do not publish a new page — people already hold this URL.</li>')
    A('</ul>')
    A('<p><strong>Public-safety rule:</strong> this page is shareable with third parties and both repositories '
      'are public. It deliberately carries no client names, instance identifiers, or exploit mechanics, and '
      'describes things generically. Keep it that way when editing the manifest.</p>')
    A('<p>A stale generation timestamp at the top is the signal that a regeneration is due.</p>')
    return "\n".join(out)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out")
    a = p.parse_args()
    m = json.load(open(os.path.join(HERE, "manifest.json")))
    body = render(m, m["repo"])
    if a.out:
        open(a.out, "w").write(body); print(f"wrote {a.out} ({len(body)} chars)", file=sys.stderr)
    else:
        print(body)
