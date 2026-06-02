# Rule: BTC payment — always output copy-paste line after send

## Trigger

Immediately after a successful `btc_wallet_send_transfer` call (i.e. response contains `"success": true` and a `txid`).

## Required behavior

1. **Output the copy-paste line in the reply**, on its own line, in this exact format:
   ```
   {amount_eur} € 📤 https://mempool.space/tx/{txid}
   ```
   Example: `60 € 📤 https://mempool.space/tx/0979bb61...`

2. **Always include it** — do not omit it even if the rest of the confirmation is brief or the session is resuming mid-flow.

3. **Store the transaction in Neotoma** in the same turn, with the txid in the `notes` field.

## Forbidden patterns

- Completing a BTC payment turn without outputting the copy-paste line.
- Deferring the copy-paste line to a follow-up turn.
- Omitting the line because the payment happened in a prior session segment.

## Rationale

The copy-paste line is used for personal bookkeeping (e.g. pasting into a spending log). It mirrors the `copy_paste_line` field produced by `BtcTransferHandler.format_confirmation()` in `execution/daemons/monedula/handlers/btc_transfer.py`.
