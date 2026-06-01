# Email draft preview (local and cross-client)

Two ways to preview email formatting: **local** (browser only) and **test send** (real clients via SendGrid/Resend).

## Local preview (quick layout check)

Renders the HTML in your default browser. Use for layout/CSS only; Gmail/Outlook apply their own quirks.

```bash
# Open HTML file in browser
python execution/scripts/email_preview_local.py path/to/draft.html

# From stdin
cat draft.html | python execution/scripts/email_preview_local.py -

# Serve on http://127.0.0.1:8765 (avoids some file:// restrictions)
python execution/scripts/email_preview_local.py path/to/draft.html --serve
```

Output is written to `tmp/email_preview.html` and opened (or served).

## Test send for accurate client preview

Send one email via your configured delivery API (Resend, SendGrid, or Mailgun) to your own address, then open it in Gmail, Outlook, Apple Mail, etc. for real client rendering.

**Generic draft (any HTML file):**

```bash
python execution/scripts/email_preview_send.py \
  --to you@example.com \
  --subject "Preview: My draft" \
  --html-file path/to/draft.html
```

Optional: `--text-file path/to/draft.txt` for plain-text part.

**Newsletter (same HTML as full send):**

```bash
python execution/scripts/newsletter_send.py \
  --issue 1 \
  --subject "Newsletter title" \
  --html-file path/to/newsletter.html \
  --preview-to you@example.com
```

Uses: `EMAIL_DELIVERY_API`, `EMAIL_DELIVERY_API_KEY`, `NEWSLETTER_FROM_EMAIL`. Override from address with `EMAIL_PREVIEW_FROM_EMAIL` for the generic script.

## NPM packages for local preview

Use these for browser (and optionally iOS Simulator) preview with a nicer UI or Nodemailer integration.

| Package | Weekly downloads | Use case |
|--------|-------------------|----------|
| **preview-email** | ~427K | Nodemailer-style message object or RFC822 string; opens browser (and iOS Simulator on macOS). Accepts `{ from, to, subject, html, text, attachments }`. |
| **@react-email/preview-server** | ~506K | Live preview for **React Email** components (JSX/TSX), not raw HTML. Use if you build emails with `react-email`. |
| **mjml** + **mjml-cli** | — | Build responsive email from MJML markup; `mjml -w input.mjml` for watch + HTML output. Use MJML Try-it-live or IDE plugins for preview. |
| **mail-preview** (vemarav) | — | Express-based; renders HTML emails in browser with theme/media-query support. Lighter than preview-email. |

**preview-email** (works with raw HTML):

```bash
npm install preview-email nodemailer
```

```js
const previewEmail = require('preview-email');
const fs = require('fs');

const html = fs.readFileSync('path/to/draft.html', 'utf-8');
previewEmail({
  from: 'preview@local',
  to: 'you@example.com',
  subject: 'Preview',
  html,
  text: html.replace(/<[^>]*>/g, ''),
}).then((url) => console.log('Opened:', url));
```

Options: `open: false` (don’t auto-open), `openSimulator: false` (no iOS Simulator), `returnHTML: true` (get HTML string only), `dir` (where to write the preview file).

**React Email** (if you author emails in React):

- `npx create-email` then `npm run email` for dev server and component preview.
- Not for one-off HTML files; use **preview-email** or the Python local script for those.

**MJML** (responsive email from markup):

- `npm install mjml` then `npx mjml -w draft.mjml` to compile to HTML and watch.
- Preview the generated HTML with `email_preview_local.py` or **preview-email**.

## SendGrid Inbox Rendering Test (many clients at once)

SendGrid’s **Email Testing** (Inbox Rendering Test) shows one send across multiple inbox clients in the dashboard. It’s available for **Marketing Campaigns** and **Dynamic Templates** and uses plan credits; there’s no simple “preview this HTML” API for arbitrary drafts. For ad-hoc drafts, use the test-send flow above and open the message in each client yourself.
