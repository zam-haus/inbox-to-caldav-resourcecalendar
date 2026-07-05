# Implementation Plan

Implementation plan for the requirements in [REQUIREMENTS.md](REQUIREMENTS.md). The marimo notebooks under `notebooks/` are exploratory prototypes and serve as reference only; they are not part of the implementation and will not be imported by it. The production code lives in a proper Python package driven from `main.py`.

## Architecture Overview

A single-process pipeline, run periodically (cron/systemd timer) or in a poll loop:

```
IMAP inbox ──> fetch & parse ──> route to resource ──> decide (policy) ──> CalDAV write
                                                            │
                                                            ├──> iMIP REPLY to organizer (SMTP)
                                                            └──> approval forward to approvers (SMTP)
```

Processing is stateless where possible: the resource calendar itself is the source of truth for existing bookings (UID lookup, status, sequence). Only the approval correlation (pending booking ↔ forwarded mail) needs a small persistent state store.

## Package Layout

```
inbox_to_caldav/
    __init__.py
    config.py        # load & validate configuration (FR-6, FR-10)
    imap_client.py   # fetch unseen messages, mark processed (FR-1)
    imip.py          # parse mail → iMIP message; extract VEVENTs, method, sender, recipients (FR-2)
    routing.py       # recipient address → resource/calendar mapping (FR-10)
    policy.py        # accept/tentative/decline decisions (FR-5, FR-6, FR-8, FR-9)
    caldav_store.py  # CalDAV access: find-by-UID, upsert, mark cancelled (FR-3, FR-4)
    approval.py      # forward-to-approvers, parse ACCEPT/REJECT replies, correlation state (FR-6)
    smtp_out.py      # send iMIP REPLY and notification mails (FR-7)
    pipeline.py      # orchestrates one run over the inbox
main.py              # CLI entry point: load config, run pipeline once or in a loop
```

## Components

### 1. Configuration (`config.py`)

TOML file (location via `--config` or `INBOX2CALDAV_CONFIG`), validated at startup:

```toml
[imap]     server, user, password, inbox, filter    # filter per RFC 3501 SEARCH
[smtp]     server, user, password, from_address     # for REPLYs and approval forwards
[caldav]   url, username, password

[[resources]]
email = "room1@example.org"          # exact, case-insensitive match (FR-6/FR-10)
calendar_url = "https://…/calendars/user/abc123/"   # by URL, never by display name (FR-10)
organizer_allowlist = ["alice@example.org"]
approvers = ["board@example.org"]
```

Secrets may alternatively come from environment variables (compatible with the existing `.env` usage). Multiple `[[resources]]` blocks share the one inbox.

### 2. IMAP fetching (`imap_client.py`)

- Connect via `imaplib.IMAP4_SSL`, select the configured mailbox, search with the configured filter plus `UNSEEN` (reference: `notebooks/from_imap.py`).
- Yield raw messages; after successful processing, mark them `\Seen` (or move to a `Processed` subfolder — decided by config) so reruns are idempotent. Failed messages are left unseen and retried next run, with a retry cap to avoid poison-message loops (flag `\Flagged` + log after N failures).

### 3. iMIP parsing (`imip.py`)

- Walk MIME parts for `text/calendar`; parse with `icalendar`.
- Extract: iTIP `METHOD`, all `VEVENT`s with UID, `SEQUENCE`, `DTSTART`/`DTEND`, `RRULE`/`RDATE`/`EXDATE`, `RECURRENCE-ID`, `ORGANIZER`, plus mail headers (`From`, `To`/`Cc`/`Delivered-To`, `Message-ID`, `In-Reply-To`/`References`).
- Classify the message: scheduling message (`REQUEST`/`CANCEL`), unsupported method (FR-11 → ignore), or plain mail (candidate approval reply → hand to `approval.py`).

### 4. Routing (`routing.py`)

- Determine the addressed resource: match configured resource addresses against `Delivered-To`, `To`, `Cc`, and the `ATTENDEE` list of the VEVENT (FR-10). First unambiguous match wins; messages matching no resource are logged and skipped.

### 5. Policy (`policy.py`)

Pure decision logic (no I/O) — the most test-worthy module. Input: parsed event, sender, resource config, current calendar state. Output: one of `ACCEPT`, `TENTATIVE`, `DECLINE(reason)`, `CANCEL`, `IGNORE`.

- Reject any VEVENT with `RECURRENCE-ID` → `DECLINE` with explanatory text (FR-5).
- Sender on the resource's organizer allowlist → `ACCEPT`, else `TENTATIVE` (FR-6).
- Updates (known UID, higher `SEQUENCE`): apply; if the original required approval, force `TENTATIVE` again (FR-8). Stale sequence → `IGNORE`.
- Conflict check: expand the requested event's occurrences over a bounded horizon (`recurring_ical_events`) and intersect with existing non-cancelled bookings in the target calendar; overlap → `DECLINE` (FR-9).

### 6. CalDAV store (`caldav_store.py`)

- One `caldav.DAVClient`; calendars resolved by URL from config (FR-10).
- `find_by_uid(uid)`, `upsert(event, status)`, `cancel(uid)`.
- Writing (reference: `notebooks/to_caldav.py`): copy the incoming VEVENT into a fresh `icalendar.Calendar`, strip `ATTENDEE`s, set a neutral `ORGANIZER`, set `STATUS` (`CONFIRMED`/`TENTATIVE`/`CANCELLED`) and `TRANSP` (`OPAQUE`; `TRANSPARENT` only when cancelled — FR-9, deliberately different from the prototype), preserve UID/SEQUENCE, save with `increase_seqno=False`.
- Never delete (FR-4): `CANCEL` handling and `REJECT` both upsert with `STATUS:CANCELLED`, dropping `EXDATE` on full-series cancels as the prototype does.

### 7. Approval flow (`approval.py`)

- On `TENTATIVE`: forward the original request to the resource's approvers. The forward carries a generated token and the event UID in subject/body; its `Message-ID` is recorded.
- Persistent correlation store (SQLite or a JSON file, path from config): `token → (resource, uid, sequence, forward message-id)`.
- Parsing replies: a mail from an approver address whose `In-Reply-To`/`References` matches a recorded forward, or whose body/subject contains a known token/UID (threading first, token fallback — FR-6). First word `ACCEPT`/`REJECT` (case-insensitive, tolerating quoted text below) decides.
- On decision: update event status via `caldav_store`, notify the organizer via `smtp_out`, mark the pending entry resolved. Replies referencing an outdated `SEQUENCE` are ignored (superseded by FR-8 re-approval).

### 8. Outgoing mail (`smtp_out.py`)

- iMIP `REPLY` per RFC 6047: `text/calendar; method=REPLY` part with the resource as `ATTENDEE;PARTSTAT=ACCEPTED|TENTATIVE|DECLINED`, sent to the organizer (FR-7).
- Human-readable explanatory text for declines (RECURRENCE-ID rejection, conflicts).
- Approval forwards to approvers.

### 9. Pipeline & CLI (`pipeline.py`, `main.py`)

- `main.py`: argparse (`--config`, `--once`/`--interval`, `--dry-run`, `--verbose`), logging setup, then `pipeline.run()`.
- `--dry-run` performs all parsing and policy decisions but skips CalDAV writes and outgoing mail — for safe testing against a live inbox.
- Process messages oldest-first (sort by `Date`) so updates apply in order.

## Dependencies

Already in `pyproject.toml` context: `icalendar`, `caldav`, `recurring_ical_events`, `python-dotenv`. Standard library covers IMAP (`imaplib`), SMTP (`smtplib`), MIME (`email`), TOML (`tomllib`), and SQLite. The notebook-only dependencies (marimo, polars, plotly, altair, joblib, icecream) are **not** used; move them to a dev/notebook dependency group.

## Testing Strategy

1. **Unit tests (pytest)** — bulk of coverage, no network:
   - `imip.py` against captured `.eml` fixtures from Google/Nextcloud/Thunderbird/Outlook (REQUEST, CANCEL, EXDATE update, RECURRENCE-ID edit, COUNTER).
   - `policy.py` decision table: allowlist × method × sequence × conflict × RECURRENCE-ID.
   - `approval.py` reply matching: threading hit, token fallback, non-approver sender, stale sequence.
2. **Integration tests** against fake servers: a scripted IMAP stub and either a local Radicale instance or the `caldav` test server for store round-trips (find-by-UID, upsert, cancel-not-delete).
3. **Manual end-to-end** against the real Nextcloud/Postfix setup using `--dry-run` first, covering the README's known quirks (trash-bin UID issue, series-edit inconsistencies).

## Milestones

1. **M1 — Skeleton & config**: package layout, config loading/validation, logging, CLI with `--dry-run`.
2. **M2 — Read path**: IMAP fetch, iMIP parsing, routing; dry-run prints decisions. (Ports the logic explored in `notebooks/from_imap.py`.)
3. **M3 — Write path**: CalDAV upsert/cancel with correct STATUS/TRANSP handling; REQUEST/CANCEL end-to-end for allowlisted organizers. (Ports and corrects `notebooks/to_caldav.py`.)
4. **M4 — Replies**: outgoing iMIP REPLY (ACCEPTED/DECLINED), RECURRENCE-ID and conflict declines.
5. **M5 — Approval workflow**: tentative bookings, approver forwards, ACCEPT/REJECT parsing, re-approval on update, correlation store.
6. **M6 — Hardening**: idempotency/retry behavior, poison-message handling, integration tests, deployment docs (systemd timer example).

## Open Points

- SMTP account details for outgoing mail are new configuration not present in the prototypes; confirm the functional mailboxes may send mail.
- Conflict-check horizon for unbounded RRULEs (proposal: 1 year ahead, configurable).
- Whether processed mails are marked `\Seen` or moved to a subfolder (proposal: subfolder, keeps the inbox as a clean queue).
