# Rule: Credential storage — always use 1Password immediately

## Trigger

Whenever credentials, license keys, API keys, tokens, secrets, or any authentication material are found during a turn — regardless of source (email, file, web page, terminal output, etc.).

## Required behavior

1. **Store in 1Password before responding.** Use `op item create` in the same turn the credential is discovered, before composing the user-visible reply.
2. **Default vault:** `Private` (ID `mf4mebzjt5nv25m72fygf26jpi`) unless the credential is clearly business/shared — then use `Confidential` (`e2pthglnr2pgyxyigtfqmkfudu`) or `Hendrickson Serrano` (`rd2yogyue6q3s35cnf4lsxolma`) as appropriate.
3. **Category mapping:**
   - Software license keys → `Software License`
   - API keys / tokens → `API Credential`
   - Username + password → `Login`
   - Other secrets → `Secure Note`
4. **Report in reply:** State the vault name and item title so the user knows where it landed.
5. **Never leave credentials only in chat.** Chat history is not a secrets store.

## Forbidden patterns

- Displaying a credential in the reply without first storing it in 1Password.
- Skipping storage because "the user already has it" or "it came from their own email".
- Creating a 1Password item after composing the reply (storage must come first).

## Example

```bash
# Correct — store first, then reply
op item create \
  --category "Software License" \
  --title "Loopback — Rogue Amoeba" \
  --vault "Private" \
  "license key=XXXX-YYYY-ZZZZ"

# Then reply: "Stored in 1Password (Private vault) as 'Loopback — Rogue Amoeba'."
```
