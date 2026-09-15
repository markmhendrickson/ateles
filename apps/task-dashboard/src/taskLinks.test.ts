/**
 * TESTS FOR THE TASK → ISSUE/PR LINK CONTRACT.
 *
 * These defend the properties that, if broken, would put a confident wrong
 * answer on the task page: an unread edge list rendering as "no linked PRs", a
 * dangling reference rendering as a working link, or a bare number standing in
 * for a title.
 *
 * The production measurements they encode (2026-09-02, live prod):
 *   - 226 of 21,362 tasks carry a URL field; 216 are GitHub issue/PR URLs
 *   - 209 of those 216 (96.8%) resolve to a stored entity — 7 dangle
 *   - the issue number lives under FOUR field names, so all four are read
 */
import { describe, expect, it } from "vitest";
import type { EntityEdge, EntityPayload } from "./entity";
import { refFromUrl, taskLinks } from "./taskLinks";

function edge(over: Partial<EntityEdge>): EntityEdge {
  return {
    entity_id: "ent_target",
    entity_type: "issue",
    canonical_name: null,
    relationship_type: "REFERS_TO",
    snapshot: null,
    direction: "outgoing",
    ...over,
  };
}

const payload = (over: Partial<EntityPayload>): EntityPayload => ({
  entity: null,
  outgoing: [],
  incoming: [],
  ...over,
});

describe("refFromUrl", () => {
  it("reads issue and PR URLs alike", () => {
    expect(refFromUrl("https://github.com/markmhendrickson/ateles/issues/714")).toBe("ateles#714");
    expect(refFromUrl("https://github.com/markmhendrickson/neotoma/pull/2163")).toBe(
      "neotoma#2163",
    );
  });

  it("is null for URLs that are not an issue or PR", () => {
    // A blob URL and a non-GitHub URL both appeared in the 10 non-issue task
    // URLs measured; neither may be turned into a reference.
    expect(refFromUrl("https://github.com/markmhendrickson/neotoma/blob/main/docs/x.md")).toBeNull();
    expect(refFromUrl("https://chatgpt.com/c/abc")).toBeNull();
    expect(refFromUrl(null)).toBeNull();
  });
});

describe("coverage — partiality is never rendered as absence", () => {
  it("reports `failed` when the relationship read failed", () => {
    const r = taskLinks(payload({ relationshipsFailed: true }), null);
    expect(r.coverage).toBe("failed");
    expect(r.links).toEqual([]);
  });

  it("reports `hydrating` while a neighbour has no entity_type yet", () => {
    // The proxy hydrates targets in the background, so the first poll can carry
    // an edge whose type is unknown. That is not the same as having read the
    // list and found no issues.
    const r = taskLinks(payload({ outgoing: [edge({ entity_type: null })] }), null);
    expect(r.coverage).toBe("hydrating");
  });

  it("reports `read` for a fully hydrated list", () => {
    const r = taskLinks(payload({ outgoing: [edge({ entity_type: "plan" })] }), null);
    expect(r.coverage).toBe("read");
    expect(r.links).toEqual([]);
  });
});

describe("link extraction", () => {
  it("keeps only issue and pull_request edges", () => {
    const r = taskLinks(
      payload({
        outgoing: [
          edge({ entity_id: "a", entity_type: "issue", snapshot: { title: "I" } }),
          edge({ entity_id: "b", entity_type: "plan", snapshot: { title: "P" } }),
          edge({ entity_id: "c", entity_type: "pull_request", snapshot: { title: "PR" } }),
        ],
      }),
      null,
    );
    expect(r.links.map((l) => l.id)).toEqual(["a", "c"]);
  });

  it("reads both directions and dedupes a reciprocal pair", () => {
    // Pre-backfill edges ran both ways depending on which agent wrote them, so
    // reading only `outgoing` would hide the older links.
    const r = taskLinks(
      payload({
        outgoing: [edge({ entity_id: "x", snapshot: { title: "T" } })],
        incoming: [edge({ entity_id: "x", direction: "incoming", snapshot: { title: "T" } })],
      }),
      null,
    );
    expect(r.links).toHaveLength(1);
  });

  it("builds a GitHub URL from any of the four issue-number fields", () => {
    // The number is split across four field names (ent_b8387e7c5756e9a2f178f088).
    // Reading only `github_number` is the bug that measured 0% resolution.
    for (const field of ["github_number", "issue_number", "github_issue_number", "number"]) {
      const r = taskLinks(
        payload({
          outgoing: [
            edge({ snapshot: { title: "T", repo: "markmhendrickson/ateles", [field]: 714 } }),
          ],
        }),
        null,
      );
      expect(r.links[0].url).toBe("https://github.com/markmhendrickson/ateles/issues/714");
      expect(r.links[0].ref).toBe("ateles#714");
    }
  });

  it("prefers the entity's own stored URL over a rebuilt one", () => {
    const r = taskLinks(
      payload({
        outgoing: [
          edge({
            snapshot: {
              title: "T",
              github_url: "https://github.com/markmhendrickson/neotoma/pull/2163",
              repo: "markmhendrickson/ateles",
              github_number: 1,
            },
          }),
        ],
      }),
      null,
    );
    expect(r.links[0].url).toBe("https://github.com/markmhendrickson/neotoma/pull/2163");
  });

  it("does not guess an owner when `repo` is a bare name", () => {
    // `repo` is sometimes "ateles" rather than "markmhendrickson/ateles".
    // A URL built from a guessed owner would 404 or, worse, hit someone else's repo.
    const r = taskLinks(
      payload({ outgoing: [edge({ snapshot: { title: "T", repo: "ateles", github_number: 9 } })] }),
      null,
    );
    expect(r.links[0].url).toBeNull();
  });

  it("does not mistake a canonical_name repo path for a title", () => {
    // `issue:347|markmhendrickson/neotoma` — the tail is a repo, not a title.
    const r = taskLinks(
      payload({ outgoing: [edge({ canonical_name: "issue:347|markmhendrickson/neotoma" })] }),
      null,
    );
    expect(r.links[0].title).toBeNull();
  });

  it("orders issues before PRs, then by reference", () => {
    const r = taskLinks(
      payload({
        outgoing: [
          edge({ entity_id: "p", entity_type: "pull_request", snapshot: { title: "PR" } }),
          edge({
            entity_id: "i",
            snapshot: { title: "I", repo: "markmhendrickson/ateles", github_number: 5 },
          }),
        ],
      }),
      null,
    );
    expect(r.links.map((l) => l.entityType)).toEqual(["issue", "pull_request"]);
  });
});

describe("dangling references — named, never fabricated", () => {
  it("surfaces a source_url that no edge covers", () => {
    // 7 of 216 measured. The issue is real on GitHub but was never stored, so
    // no relationship could point at it.
    const r = taskLinks(
      payload({}),
      "https://github.com/markmhendrickson/ateles/issues/94",
    );
    expect(r.sourceUrlOnly).toBe("https://github.com/markmhendrickson/ateles/issues/94");
    expect(r.links).toEqual([]);
  });

  it("stays quiet when an edge already covers the source_url", () => {
    const r = taskLinks(
      payload({
        outgoing: [
          edge({
            snapshot: {
              title: "T",
              github_url: "https://github.com/markmhendrickson/ateles/issues/94",
            },
          }),
        ],
      }),
      "https://github.com/markmhendrickson/ateles/issues/94",
    );
    expect(r.sourceUrlOnly).toBeNull();
    expect(r.links).toHaveLength(1);
  });

  it("ignores a non-issue source_url", () => {
    const r = taskLinks(payload({}), "https://chatgpt.com/c/abc");
    expect(r.sourceUrlOnly).toBeNull();
  });
});
