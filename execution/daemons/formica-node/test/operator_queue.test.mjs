import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { OperatorQueue } from "../src/operator_queue.mjs";

describe("operator_queue", () => {
  it("delivers to waiter before buffer", async () => {
    const q = new OperatorQueue();
    const p = q.waitLine("ent_a", 5000);
    q.enqueue("ent_a", "hello");
    assert.equal(await p, "hello");
  });

  it("buffers then waitLine receives", async () => {
    const q = new OperatorQueue();
    q.enqueue("ent_b", "first");
    assert.equal(await q.waitLine("ent_b", 5000), "first");
  });
});
