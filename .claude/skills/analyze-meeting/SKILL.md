---
name: analyze-meeting
description: "Deprecated alias for process-meeting. Meeting work now runs through the fuller processing pipeline: raw-material preservation, best-fidelity transcription with a learned transcription profile, matter and participant resolution, graph reconciliation against existing entities, swarm task creation and update, per-topic strategic sub-analyses, participant argument evaluation, proactive external research, a meeting-value plus relationship-significance assessment, and recommendations (never drafts) for follow-up messages and a participant-facing recap page. Invoking this skill forwards to process-meeting with the same arguments."
triggers:
  - /analyze-meeting
user_invocable: true
superseded_by: process-meeting
deprecated: true
---

# analyze-meeting (deprecated alias)

This skill has been superseded by [`process-meeting`](../process-meeting/SKILL.md).

**Do this now:** invoke `process-meeting` with the arguments this skill received, unchanged, and follow that skill in full. Do not perform any analysis here.

## What carries over

`--open-issues` and `--participants` behave identically. Source auto-detection is a superset of the old behavior: file paths, `transcription` entity ids, and pasted text all still work, and raw audio/video files are now accepted too.

`process-meeting` adds `--no-research`, `--retranscribe`, and `--depth quick|standard|deep`.

## What changed

**`--recap` is gone, and no message is ever drafted.** The prior skill drafted recap messages when `--recap` was passed and staged them as Gmail drafts. `process-meeting` drafts nothing outbound under any flag. Instead it recommends which follow-up messages are worth sending — recipient, channel, intent, the points to cover, tone, and timing — as `recommended_message` entities each backed by a task. The writing stays with the operator or a later drafting run.

It also recommends a participant-facing recap **rendered page** when the meeting warrants one (multi-party, structured content, a matter with continuity), as a `recommended_rendered_page` entity plus a task, routed to the appropriate Neotoma instance. It does not build or publish the page; [`draft-rendered-page`](../draft-rendered-page/SKILL.md) does that when the operator runs the task.

Consequently `process-meeting` creates **no `recap_message` entities**. Existing ones from prior runs stay valid and readable.

## Continuity

All other outputs are still produced — `meeting_analysis`, `task`, `proposed_github_issue`, `calendar_event` — alongside the new `transcription_profile`, `meeting_topic_analysis`, `participant_position`, `research_inquiry`, `research_finding`, `meeting_value_assessment`, `recommended_message`, and `recommended_rendered_page` entities.

Existing `meeting_analysis` entities remain valid and are extended in place by re-runs; the per-meeting idempotency key is unchanged, so re-running `process-meeting` on a previously analyzed transcript deepens that analysis rather than duplicating it.
