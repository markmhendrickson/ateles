import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { flattenEntitySnapshot, heuristicClassify } from "../src/classifier.mjs";

describe("classifier", () => {
  it("flattens nested snapshot", () => {
    const f = flattenEntitySnapshot({
      entity_id: "ent_x",
      entity_type: "issue",
      snapshot: { title: "Hello", body: "World" },
    });
    assert.equal(f.title, "Hello");
    assert.equal(f.body, "World");
    assert.equal(f.entity_id, "ent_x");
  });

  it("heuristic question", () => {
    const c = heuristicClassify({ title: "How do I configure OAuth?" });
    assert.equal(c.classification, "question");
  });
});
