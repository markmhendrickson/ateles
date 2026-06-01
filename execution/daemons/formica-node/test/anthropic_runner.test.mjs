import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { runConversationalAnthropic } from "../src/anthropic_runner.mjs";

describe("anthropic_runner", () => {
  it("returns error when API key missing", async () => {
    const r = await runConversationalAnthropic({
      worktreePath: "/tmp/wt",
      prompt: "hi",
      apiKey: "",
      fetchImpl: async () => new Response("{}"),
    });
    assert.equal(r.ok, false);
    assert.equal(r.error, "ANTHROPIC_API_KEY_required");
  });

  it("stops on DONE in first response", async () => {
    let calls = 0;
    const fetchImpl = async () => {
      calls++;
      return new Response(
        JSON.stringify({
          id: "msg_1",
          type: "message",
          role: "assistant",
          content: [{ type: "text", text: "All set.\nDONE" }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    };
    const r = await runConversationalAnthropic({
      worktreePath: "/tmp/wt",
      prompt: "fix",
      apiKey: "test-key",
      model: "claude-test",
      fetchImpl,
    });
    assert.equal(r.ok, true);
    assert.equal(calls, 1);
    assert.match(r.stdout || "", /DONE/);
    assert.equal(r.anthropic_message_id, "msg_1");
  });

  it("honors operator /shipit after first reply without DONE (no second API call)", async () => {
    let calls = 0;
    const fetchImpl = async () => {
      calls++;
      return new Response(
        JSON.stringify({
          id: "msg_2",
          content: [{ type: "text", text: "Partial work only." }],
        }),
        { status: 200 },
      );
    };
    const r = await runConversationalAnthropic({
      worktreePath: "/tmp/wt",
      prompt: "x",
      apiKey: "k",
      fetchImpl,
      pollOperator: async () => "/shipit",
    });
    assert.equal(r.ok, true);
    assert.equal(calls, 1);
    assert.equal(r.operator_stop, "/shipit");
  });
});
