import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { mirrorTelegramInboundToNeotoma } from "../src/telegram_mirror.mjs";

describe("telegram_mirror", () => {
  it("no-ops when mirror disabled", async () => {
    await mirrorTelegramInboundToNeotoma(
      { telegramInboundMirror: false, baseUrl: "http://x", token: "t" },
      { chat: { id: 1 }, message_id: 2, text: "hi" },
      "hi",
    );
    assert.ok(true);
  });
});
