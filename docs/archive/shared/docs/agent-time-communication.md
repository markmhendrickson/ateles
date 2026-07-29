# Agent Time & Communication Rules

**Purpose:** Rules for time-sensitive operations and communication follow-up requirements.

**Last Updated:** 2025-01-23

---

## Time-Sensitive Operations

**MANDATORY:** Always check the current date and time when executing time-sensitive tasks.

**When to verify current date/time:**
- Discussing task due dates or deadlines
- Scheduling or updating tasks with time components
- Making time-sensitive recommendations or decisions
- Comparing dates in any context (e.g., "tomorrow", "next week", "overdue")
- Calculating time-based metrics or intervals

**Rationale:** Ensures accurate due date interpretation, prevents scheduling errors, and maintains correct time-based decision making.

**Implementation:** Use `date` command or system date/time APIs to verify current date before making any time-sensitive statements or recommendations.

---

## Communication Follow-Up Requirements

**MANDATORY:** When emitting communication that requires a response to complete a task, always create a subtask for checking in with the other party.

**When to create follow-up subtask:**
- Sending emails that require a response to proceed with a task
- Submitting forms or applications that require confirmation
- Requesting information or documents from third parties
- Any communication where task completion depends on external response

**Follow-up subtask requirements:**
1. **Link to parent task:** Set `parent_task_id` to the main task that requires the response
2. **Set due date:** Calculate due date as (expected response time + buffer). For example:
   - If response expected in 2 business days → set due date to 4-5 business days
   - If response expected in 1 week → set due date to 1.5-2 weeks
   - Always add buffer time beyond stated response window
3. **Include context in notes:**
   - Original communication details (email ID, date, thread ID if applicable)
   - Expected response time/window
   - Contact information (email, phone, address)
   - Action plan if no response received (follow-up email, phone call, etc.)
   - Reference to email drafts or communication files
4. **Task metadata:**
   - Status: `pending`
   - Priority: `medium` (adjust based on urgency of parent task)
   - Urgency: `soon` or `this_week` (based on due date)
   - Domain: Match parent task domain

**Rationale:** Ensures no communication requiring a response falls through the cracks. Provides automatic reminder to follow up if response is delayed, maintaining task momentum and preventing blockers.

**Example:**
- Parent task: "Submit form to Agency X"
- Communication: Email sent with form submission
- Expected response: Within 2 business days
- Follow-up subtask: "Follow up with Agency X - Form submission response"
- Due date: 5 business days after submission (2 days expected + 3 day buffer)






