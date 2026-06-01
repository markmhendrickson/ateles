# Browser agent directive: configure Cloudflare API token

Use this document when automating the Cloudflare dashboard in a browser to create an API token for zone Bot Management and AI crawler / `robots.txt` changes via the API.

## Goal

Create a **Cloudflare API token** with permissions to read the zone and update **Bot Management** (managed `robots.txt`, AI bots protection), then surface the token value for the user to store securely (e.g. 1Password, shell env `CLOUDFLARE_API_TOKEN`).

## Preconditions

- User is logged into Cloudflare in the browser session (or agent completes login first).
- Target zone: **markmhendrickson.com** (adjust hostname in steps if different).

## Execution order

1. **Open API tokens**

   - Navigate to: `https://dash.cloudflare.com/profile/api-tokens`
   - Wait until the page shows **API Tokens** and a **Create Token** (or **Create Custom Token**) control.

2. **Start custom token creation**

   - Click **Create Token**.
   - If templates are shown, choose **Create Custom Token** (or equivalent) so permissions can be set explicitly.

3. **Set token name**

   - Set **Token name** to something identifiable, e.g. `ateles-bot-management-markmhendrickson`.

4. **Set permissions**

   **Critical — scope is Zone, not Account:** The Bot Management API is tied to **zone** settings. The first column of each permission row must be **Zone** for bot/`robots.txt` API access. If the first column is **Account** and you search `bot`, Cloudflare shows unrelated items (e.g. **DDoS Botnet Feed**, load balancers, Workers builds). **Do not use those** for this task.

   Add rows in this order (each row: left = resource type, middle = permission name, right = access):

   | Row | Left (scope) | Middle (permission) | Right (access) |
   |-----|--------------|---------------------|----------------|
   | A | **Zone** | **Bot Fight Mode** *or* **Bot Management** | **Edit** |
   | B | **Zone** | **Zone** | **Read** |

   **Which middle option?** Depends on plan and UI:

   - After **Zone**, search `bot`. You may see **Bot Management** (common on paid / BM-enabled zones), **Bot Fight Mode** (common on free Bot Fight Mode), or **Bot Management Feedback** (not sufficient alone for config API—avoid as the only bot row).
   - Pick **Bot Management** or **Bot Fight Mode** and set the third column to **Edit** (not **Select…**). **Read** is enough only for GET; changing managed `robots.txt` or AI bot rules needs **Edit**.

   Optional: if you also need DNS changes on the same token, add **Zone** → **DNS** → **Edit** (still **Zone** in the first column).

   **How to find the permission:** Set the **first** dropdown to **Zone**, then in the **second** dropdown type `bot` or `fight` and choose **Bot Management** or **Bot Fight Mode** as offered.

5. **Zone resources**

   - Under **Zone Resources**, restrict to **Include** → **Specific zone** → select **markmhendrickson.com** (or the correct zone).

6. **Create and copy**

   - Click **Continue to summary**, review, then **Create Token**.
   - **Copy the token string once** when Cloudflare shows it. It will not be shown again.

7. **Handoff to user**

   - Tell the user to store the token in a secret manager and export for CLI/API use, for example:

     ```bash
     export CLOUDFLARE_API_TOKEN='<paste-token-here>'
     ```

   - Optional: resolve zone ID for scripts:

     ```bash
     curl -sS "https://api.cloudflare.com/client/v4/zones?name=markmhendrickson.com" \
       -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
       -H "Content-Type: application/json" | jq -r '.result[0].id'
     ```

## Verification (after token exists)

- **List zones** (read check):

  ```bash
  curl -sS "https://api.cloudflare.com/client/v4/zones?name=markmhendrickson.com" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json"
  ```

  Expect `success: true` and a `result` array with the zone.

- **Read bot management** (permission check):

  ```bash
  curl -sS "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/bot_management" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/json"
  ```

  Expect `success: true`. If `403` or permission errors, return to step 4 and add **Edit** on the bot-related zone permission.

## Do not

- Paste the raw token into chat logs, tickets, or git commits.
- Leave the token visible on screen after the user has copied it; close or navigate away after confirmation.

## UI variance and common mistakes

- **Wrong:** **Account** + search `bot` → pick **DDoS Botnet Feed** or other account-level items. Those do **not** grant `PUT /zones/{id}/bot_management`.
- **Right:** **Zone** + **Bot Fight Mode** + **Edit**, plus **Zone** + **Zone** + **Read**, with **Zone Resources** including the target hostname (**neotoma.io**, **markmhendrickson.com**, etc.).

If **Bot Fight Mode** is missing after selecting **Zone**:

- Confirm the token’s **Zone Resources** include that exact zone.
- Try synonyms in the middle dropdown (`fight`, `bot fight`).
- Fall back to Cloudflare docs: [API: Update Zone Bot Management](https://developers.cloudflare.com/api/resources/bot_management/methods/update/) and match dashboard labels to that API.

## Related

- Managed `robots.txt` behavior: [Cloudflare docs — robots.txt setting](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/)
- API body fields include `is_robots_txt_managed`, `ai_bots_protection`, `cf_robots_variant` (plan-dependent).
