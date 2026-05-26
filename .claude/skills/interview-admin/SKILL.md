---
name: interview-admin
description: "Administer interviews agentically with Neotoma as canonical CRM, including provisioning, invite delivery, lifecycle sync, and funnel reporting."
triggers:
  - interview admin
  - administer interview app
  - provision interview contact
  - send interview invite
  - sync interview results
  - sync interview events
  - interview funnel
user_invocable: true
entity_id: ent_3627712a0eee47ac2a608b29
---

# interview-admin

## Purpose

Operate the interviews app without manual admin UI, with Neotoma as canonical system of record for contacts, invite lifecycle, and interview progress.

## Systems

- Canonical CRM and lifecycle state: `user-neotoma` MCP
- Remote interview app operations: `interviews-admin` MCP

## Entity model in Neotoma

- `interview_participation`
  - `interview_slug`, `share_code`, `share_url`, `contact_name`
  - `status`: `provisioned` | `invite_sent` | `opened` | `started` | `in_progress` | `abandoned` | `completed`
  - `invite_method`, `invite_sent_at`, `invite_confirmed_at`
- `interview_assessment`
  - full: normalized assessment payload from interview app result
  - partial: `is_partial: true`, transcript fields, `message_count`
- `interview_event`
  - `event_type`: invite + lifecycle events from interview app
  - `timestamp`, `session_id`, `share_code`, `interview_slug`, `message_count?`

## Workflow: Provision contact

1. Retrieve contact in Neotoma (`retrieve_entity_by_identifier`).
2. Generate deterministic share code from contact name (lowercase alnum, 2-20 chars).
3. Check remote contact list (`interviews_admin_list_contacts`).
4. Upsert remote contact (`interviews_admin_upsert_contact`) with `code`, `name`, and `email` when available.
5. Store `interview_participation` in Neotoma with status `provisioned`.
6. Create `REFERS_TO` from participation to contact.
7. Return link: `https://interviews.markmhendrickson.com/{interview_slug}/{share_code}`.

## Workflow: Send invite (email)

1. Verify participation exists and contact has an email.
2. Call `interviews_admin_send_invite`.
3. Update participation status to `invite_sent`, set `invite_method=email`, `invite_sent_at`.
4. Store `interview_event` with `event_type=invite_email_sent`.

## Workflow: Prepare text invite

1. Verify participation exists.
2. Call `interviews_admin_get_text_invite`.
3. Display the exact message for copy/paste.
4. Store `interview_event` with `event_type=invite_text_prepared`.
5. Wait for user confirmation.

## Workflow: Confirm manual text send

1. Call `interviews_admin_confirm_text_invite`.
2. Update participation to `invite_sent`, `invite_method=text`, set `invite_sent_at` and `invite_confirmed_at`.
3. Store `interview_event` with `event_type=invite_text_confirmed`.

## Workflow: Sync results

1. Pull remote results (`interviews_admin_list_results`).
2. For each `sessionId`, check for existing `interview_assessment` in Neotoma.
3. For new full results: store `interview_assessment` with `is_partial=false`; mark participation `completed`.
4. For new partial results: store `interview_assessment` with `is_partial=true`; mark participation `abandoned`.

## Workflow: Sync lifecycle events

1. Pull remote events (`interviews_admin_list_events`).
2. Deduplicate by event payload signature.
3. Store new `interview_event` entities and create `PART_OF` links.
4. Update participation status from latest event chronology.

## Workflow: Detect stale in-progress sessions

1. Query participations in `started` or `in_progress`.
2. Resolve latest related event timestamp.
3. If older than 48h (default), mark as `abandoned`.

## Workflow: Contact status

Return per-contact timeline:
- provisioning + invite metadata
- opened/started/progressed activity
- completed or abandoned state
- message depth (`message_count`) when available

## Workflow: Funnel dashboard

Aggregate participation counts by status and include:
- invite split by method (`email`, `text`)
- abandoned count and average abandoned `message_count`
- completion count
