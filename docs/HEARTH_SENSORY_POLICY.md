# HEARTH SENSORY POLICY

**Version 1.0 — June 30, 2026**

---

## Purpose

This document defines what Hearth is permitted to observe within Pathway Portal. It exists so that every future watcher, signal, or data source can be checked against a single standard instead of being decided ad hoc.

Hearth is organizational intelligence. It exists to help managers and coaches understand and support creators. It is not a surveillance system, and it does not exist to monitor private human conversation.

---

## Why Hearth Observes

Hearth does not observe information because it can. It observes information only when doing so helps managers better support creators and understand the health of the organization. Observation should always serve understanding, never surveillance.

---

## Category A — Observable

Hearth may observe, ingest, classify, and reason over all of the following:

### Creator Activity and Progress

- Training views, comments, and replies
- Check-ins, check-in answers, and check-in comments
- Onboarding steps and status
- Page visits
- Battle requests, battles, and confirmations
- Event signups and event participation
- Community posts and comments
- Coach hub and navigator hub activity
- Discord connection status

### Organizational Structure and Context

- User profiles, roles, and status
- Coach assignments (CN and Shop)
- Recruiter/navigator relationships
- Creator notes and shop creator notes
- Sticky notes
- Support threads and support messages
- Announcements and banner messages
- Pathway events and event calendar
- Admin tasks (if useful to organizational awareness)

The default posture for Category A is **yes** — if it's organizational activity happening inside Pathway, Hearth is allowed to see it. New organizational features should be added to Hearth's awareness as a matter of course, not treated as exceptions requiring special justification.

---

## Category B — Permanently Excluded

Hearth must never observe, ingest, classify, store, or reason over:

- Private direct messages between creators (content or metadata)
- Any future private 1:1 communication feature between creators
- Personal conversations that are not organizational in nature

This exclusion applies **even at the metadata level**. Hearth should not know that a private conversation occurred, who was involved, or when. Not the content. Not the timestamp. Not the existence of the message.

This is not a temporary limitation. It is a **permanent architectural and ethical boundary**. No future feature, watcher, or business need should override this without an explicit, deliberate policy change at the ownership level (Brian/Stacy).

---

## Principle of Minimum Necessary Awareness

Hearth should observe only the information necessary to understand the health, relationships, and operation of the organization.

When two data sources provide equivalent organizational understanding, Hearth should prefer the less intrusive source.

More data is not automatically better data.

---

## How to Use This Policy

Before adding any new Hearth watcher, signal, or data source, ask:

> *"Is this organizational activity, or is it private human conversation between two individuals?"*

| Classification | Answer | Action |
|---|---|---|
| Organizational activity | Category A | Build it. |
| Private conversation | Category B | Do not build it. Stop. |
| Genuinely unclear | — | Default to NOT observing until a deliberate decision is made. |

Privacy exclusions are the only place where "leave it alone until we're sure" is the correct default. Everything else defaults to visibility.

Even within Category A, apply the **Principle of Minimum Necessary Awareness** — prefer the least intrusive signal that still achieves real organizational understanding.

---

## Revision History

| Version | Date | Notes |
|---|---|---|
| v1.0 | June 30, 2026 | Initial policy established following the Hearth Awareness Audit, which found Hearth had unintentionally gained visibility into private DM metadata. That visibility was removed and this policy was written to prevent recurrence. |
