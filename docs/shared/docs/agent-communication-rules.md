# Agent Communication Rules

**Status:** Active  
**Last Updated:** 2026-01-14  
**Related:** `/shared/docs/agent-context.md`, `/shared/docs/agent-confirmation-requirements.md`

---

## Purpose

This document defines mandatory formatting and language rules for agent-generated communications (emails, messages, transaction references, calendar events).

---

## Spanish Email Formatting Rules

**MANDATORY:** When drafting Spanish emails, follow these formatting rules:

### Sign-Off

- Always use "Saludos" (never "Saludos cordiales", "Atentamente", or other variations)

### Footer

- Never include NIF/NIE in email footers or signatures

### Greeting

Use time-appropriate greeting:
- "Buenos días" - morning (until approximately 13:00)
- "Buenas tardes" - afternoon/evening (from approximately 13:00)
- Check current time before drafting to ensure appropriate greeting

**Rationale:** Maintains consistent, professional Spanish email formatting aligned with user preferences.

### Formality Level

**MANDATORY:** Use natural, conversational Spanish appropriate for Barcelona. Avoid overly formal terms:

- **Meetings:** Use "tener/hacer la reunión" instead of "celebrar la reunión" for routine business meetings
- **Prefer natural expressions** over formal/literary language in business communications
- **Match the tone** of the recipient (respond in kind to their formality level)

**Rationale:** Barcelona Spanish tends to be more direct and less formal. Overly formal language can sound unnatural or unnecessarily distant in routine business contexts.

---

## Spanish WhatsApp Style Rules

For Spanish WhatsApp-style drafts:

- Do **not** use greetings like "Hola" or name+comma (e.g., "Ana,") at the start of the message
- Always omit leading inverted question marks (¿) even when writing questions
- Keep tone direct and functional, aligned with the user's preferences

---

## Transaction Reference Language Rules

**MANDATORY:** Use language-appropriate transaction references, descriptions, and notes based on context:

### Language Selection

- **Catalan entities/organizations:** Use Catalan for references (e.g., "Donació a AVVAAPSJ", "Associació de Veïns")
- **Spanish entities/organizations:** Use Spanish for references (e.g., "Donación a...", "Asociación...")
- **International entities:** Use English for references

### Regional Context

Match the language of the recipient organization's primary language:
- Catalan for Catalan regions
- Spanish for Spanish regions

### Contact-Specific Preferences

Check the contact's `language` field in `$DATA_DIR/contacts/contacts.parquet` for language preferences. If a contact has a specified language preference (e.g., "Spanish", "Catalan", "English"), use that language for all transfers, communication, and interactions with that contact.

**Precedence:** Contact-specific preferences (stored in the contact's `language` field) take precedence over general regional rules.

**Rationale:** Ensures transaction references are appropriate for the context and maintain consistency with the recipient's language and regional practices.

---

## Calendar Event Language Rules

**MANDATORY:** Use language-appropriate descriptions and notes for calendar events based on context:

### Language Selection

- **Spanish participants/entities:** Use Spanish for event descriptions and notes (e.g., meetings in Spain, with Spanish-speaking participants)
- **Catalan participants/entities:** Use Catalan for event descriptions and notes (e.g., meetings in Catalonia, with Catalan-speaking participants)
- **International participants:** Use English for event descriptions and notes
- **Mixed participants:** Use the primary language of the meeting location or organizer

### Regional Context

Match the language of the event location and participants:
- Spanish for events in Spain (outside Catalonia) or with Spanish-speaking participants
- Catalan for events in Catalonia or with Catalan-speaking participants
- English for international events or English-speaking participants

### Contact-Specific Preferences

Check the contact's `language` field in `$DATA_DIR/contacts/contacts.parquet` for language preferences. If attendees have specified language preferences, use the primary language of the majority of attendees or the event location.

**Precedence:** Contact-specific language preferences (stored in the contact's `language` field) take precedence over general regional rules.

**Rationale:** Ensures event descriptions and notes are appropriate for the context and maintain consistency with participants' language preferences and regional practices.

### Attendee Invitations

**MANDATORY:** Always confirm with the user before inviting others (adding attendees) to calendar events, unless the user has already explicitly specified which attendees to invite.

**Rationale:** Prevents unwanted invitations and ensures user control over who receives calendar event invitations.

---

## Draft Display Rules

**MANDATORY:** When creating email drafts, messages, or any draft content, always display the draft content to the user immediately after creation.

### Display Requirements

- Show the complete draft content (subject, recipients, body) in the response
- Include draft ID or location if applicable (e.g., Gmail draft ID)
- Display drafts even if they are automatically saved to drafts folder
- Present drafts in a clear, readable format

**Rationale:** Ensures user can review and approve draft content before sending, maintaining user control over all communications.

---

## Related Documentation

- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)
- `/shared/docs/agent-persistence-requirements.md` - Contact persistence requirements






