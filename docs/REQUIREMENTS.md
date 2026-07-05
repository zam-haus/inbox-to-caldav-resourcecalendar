# Requirements

Requirements for `inbox-to-caldav-resourcecalendar`, derived from the [README](../README.md).

## Purpose

A Python script that acts as a resource calendar: it reads scheduling messages from an email inbox and maintains corresponding events in CalDAV calendars, serving as a replacement for other resource calendar implementations.

### Problem statement

Nextcloud calendars cannot accept invitations automatically, and Nextcloud resource calendars can, but cannot be viewed by users. (A Nextcloud PR exists that copies iMIP mails into the calendar automatically, but it could not be made to work in our deployment.)

The intended deployment is one functional email address per room/workshop, each paired with a normal (viewable) Nextcloud calendar that this script maintains. Several such addresses may be delivered into a single shared inbox (see FR-10).

## Use Cases

Actors: **Organizer** (a person booking a resource via their own calendar client), **Resource calendar** (the CalDAV calendar maintained by this script), **Approver** (a person on the approver list who may confirm or reject bookings), **Administrator** (an authorized CalDAV/Nextcloud user), **Script** (this software, acting as the scheduling agent for the resource).

### UC-1: Book a resource (trusted organizer)
An organizer whose email address is on the trusted-organizer list invites the resource's email address to an event from their calendar client (Google Calendar, Nextcloud, Thunderbird, …). The client sends an iMIP `REQUEST` message to the resource's mailbox. The script creates a corresponding **accepted** event in the resource calendar and sends an iMIP `REPLY` (`ACCEPTED`) to the organizer.

### UC-1b: Book a resource (unknown organizer, pending approval)
An organizer whose email address is not on the trusted-organizer list invites the resource. The script creates the event as **tentative** in the resource calendar, sends an iMIP `REPLY` (`TENTATIVE`) to the organizer, and forwards the request to the approvers.

### UC-1c: Approve or reject a pending booking
An approver replies to the forwarded request with `ACCEPT` or `REJECT`. On `ACCEPT` the script confirms the tentative event and notifies the organizer (`REPLY`, `ACCEPTED`); on `REJECT` it marks the event cancelled (per FR-4, no deletion) and notifies the organizer (`REPLY`, `DECLINED`).

### UC-2: Update a booking
The organizer changes an existing event (time, duration, summary, …) in their client, which sends an updated `REQUEST` with the same UID and an increased `SEQUENCE`. The script updates the existing event in the resource calendar accordingly. If the original booking required approval (UC-1b), the updated event reverts to **tentative** and must be approved again (UC-1c).

### UC-2b: Conflicting booking (declined)
An organizer requests a slot that overlaps an existing (accepted or tentative) booking. The script declines the request with an iMIP `REPLY` (`DECLINED`) and does not add it to the resource calendar.

### UC-3: Cancel a booking
The organizer removes the resource or deletes the event; the client sends an iMIP `CANCEL` message. The script marks the corresponding event in the resource calendar as `CANCELLED` (and transparent for free/busy) instead of deleting it (see FR-4).

### UC-4: Book a recurring slot
The organizer creates a recurring event (`RRULE`, optionally `RDATE`) inviting the resource. The script creates the recurring event in the resource calendar.

### UC-5: Exclude single occurrences from a series
The organizer deletes individual occurrences of a recurring booking. The client sends a series update carrying `EXDATE` entries; the script applies the update so the excluded occurrences no longer appear.

### UC-6: Attempt to move/edit a single occurrence (rejected)
The organizer moves or edits one occurrence of a recurring booking, producing a `VEVENT` with `RECURRENCE-ID`. The script rejects this update (see FR-5) and sends an explanatory decline mail so the organizer knows the change did not take effect. As a workaround the organizer deletes the occurrence (UC-5) and books a new, independent event (UC-1) at the desired time.

### UC-7: View resource availability
Users with access to the resource calendar view current and upcoming bookings via any CalDAV client or the Nextcloud Web UI. Cancelled bookings remain visible but are marked `CANCELLED` and do not block the time slot.

### UC-8: Permanently remove cancelled events
An administrator manually deletes cancelled events from the resource calendar (e.g. via the Nextcloud Web UI), since the script itself never deletes (FR-4).

## Functional Requirements

### FR-1: Inbox processing
The script MUST read incoming messages from an IMAP inbox.

### FR-2: iMIP/iTIP compliance
The script MUST process scheduling messages according to:
- RFC 6047 / RFC 2447 — iCalendar Message-Based Interoperability Protocol (iMIP)
- RFC 5546 / RFC 2446 — iCalendar Transport-Independent Interoperability Protocol (iTIP)

### FR-3: Calendar population
The script MUST create and update events in CalDAV calendars based on the scheduling messages received.

### FR-4: Cancellation instead of deletion
The script MUST NOT delete events from the CalDAV server. Cancelled events MUST instead be marked with status `CANCELLED`. Actual deletion is left to an authorized calendar user performing it manually.

**Rationale:** Nextcloud moves deleted events into a trash bin that cannot hold duplicates with the same UID. Importing an event, deleting it, and re-importing it with the same UID makes the event undeletable via both CalDAV and the Web UI.

### FR-5: Rejection of edited occurrences
The script MUST reject `VEVENT` components that have `RECURRENCE-ID` set (i.e., edited occurrences of a recurring series).

The script MUST still accept updates to a series that use `EXDATE` (excluded dates/times).

**Rationale:** Clients behave inconsistently when a series containing moved or deleted occurrences is subsequently edited as a whole (Nextcloud Web UI hangs; Thunderbird, Android clients, Google Calendar Web, and Outlook each resolve the conflict differently).

**Workaround for users:** delete the occurrence via `EXDATE` and create a new, unrelated event with a different UID and time on that day.

### FR-6: Allowlist-based acceptance
The script MUST maintain two separate allowlists:
- a **trusted-organizer list**: invitations from these addresses are accepted automatically;
- an **approver list**: invitations from all other addresses are accepted **tentatively** and forwarded to these addresses for approval; a mail reply containing `ACCEPT` or `REJECT` from an approver address confirms or rejects the pending calendar entry.

Approval replies are matched to the pending booking primarily via mail threading (`In-Reply-To`/`References` of the forwarded approval mail), with a fallback to an event UID or token quoted in the reply body when threading headers are missing.

Both lists are stored in a configuration file (e.g. TOML/YAML) next to the script, edited by the administrator and read on each run. Entries are full email addresses matched exactly (case-insensitive); no domain wildcards or alias normalization.

### FR-7: Organizer notification (iMIP replies)
The script MUST answer scheduling requests with an iMIP `REPLY` on behalf of the resource: `ACCEPTED` for auto-accepted or approved bookings, `TENTATIVE` for bookings pending approval, `DECLINED` for rejected or conflicting bookings. Rejections of `RECURRENCE-ID` events (FR-5) MUST include an explanatory decline mail.

### FR-8: Re-approval on update
When an event that required approval is updated by its organizer (same UID, increased `SEQUENCE`), it MUST revert to tentative and go through the approval flow again.

### FR-9: Conflict handling
The script MUST decline booking requests that overlap an existing accepted or tentative booking (iMIP `REPLY` with `DECLINED`). Events stored in the resource calendar MUST be marked `OPAQUE` so they block the time slot in free/busy views; cancelled events are set `TRANSPARENT`.

### FR-10: Multiple resources per inbox
A single IMAP inbox MAY receive mail for several resource addresses (e.g. via aliases or catch-all delivery). The script MUST:
- determine the addressed resource from the recipient information of each message (e.g. `To`/`Cc`/`Delivered-To` headers and the `ATTENDEE` matching a configured resource address);
- route the event to that resource's calendar based on a configured mapping of resource email address → CalDAV calendar;
- identify target calendars by a unique identifier (calendar URL or ID), **not** by display name, since multiple calendars may share the same name.

Messages that cannot be attributed to any configured resource address are ignored.

### FR-11: Unsupported iTIP methods
Only the iTIP methods `REQUEST` and `CANCEL` are processed. All other methods (`COUNTER`, `REFRESH`, `ADD`, `DECLINECOUNTER`, …) are silently ignored.

## Security Requirements

### SR-1: Sender authenticity
Trust in the allowlist relies on the receiving mail server enforcing DKIM/SPF/DMARC and rejecting mail that fails these checks. The script itself does not re-verify sender authenticity; it MUST therefore only be deployed behind a mail server with such enforcement.

## Compatibility Requirements

The script MUST work with the following tested environment:

| Component | Tested with |
|---|---|
| CalDAV server | Nextcloud |
| IMAP server | Postfix |
| Calendar clients / iMIP senders | Google Calendar, Nextcloud, Thunderbird |

## Non-Functional Requirements

- Implemented in Python (Python 3; the similar prior work [imip-agent](https://groupware.boddie.org.uk/imip-agent/) is Python 2 based and not reused).

## References

- [RFC 6047](https://datatracker.ietf.org/doc/html/rfc6047), [RFC 2447](https://datatracker.ietf.org/doc/html/rfc2447) (iMIP)
- [RFC 5546](https://datatracker.ietf.org/doc/html/rfc5546), [RFC 2446](https://datatracker.ietf.org/doc/html/rfc2446) (iTIP)
