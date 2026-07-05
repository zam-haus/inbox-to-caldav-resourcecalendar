"""Parse raw mail into iMIP scheduling messages (FR-2)."""

from __future__ import annotations

import email
import email.utils
import logging
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message

import icalendar

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = {"REQUEST", "CANCEL"}


@dataclass
class ParsedMail:
    """A fetched mail with any iMIP payload it carries."""

    message_id: str
    sender: str  # normalized From address
    recipients: list[str]  # normalized To/Cc/Delivered-To addresses
    subject: str
    date: datetime | None
    in_reply_to: list[str]  # In-Reply-To + References message ids
    body_text: str
    method: str | None = None  # iTIP METHOD, None if no calendar part
    calendar: icalendar.Calendar | None = None
    events: list[icalendar.Event] = field(default_factory=list)

    @property
    def is_scheduling(self) -> bool:
        return self.method in SUPPORTED_METHODS

    @property
    def is_unsupported_scheduling(self) -> bool:
        return self.method is not None and self.method not in SUPPORTED_METHODS


def _addresses(msg: Message, *headers: str) -> list[str]:
    values = []
    for header in headers:
        values.extend(msg.get_all(header, []))
    return [addr.lower() for _name, addr in email.utils.getaddresses(values) if addr]


def _body_text(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_content_disposition() == "attachment":
            try:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            except (LookupError, AttributeError):
                continue
    return ""


def _calendar_part(msg: Message) -> icalendar.Calendar | None:
    for part in msg.walk():
        if part.get_content_type() == "text/calendar":
            raw = part.get_payload(decode=True)
            try:
                return icalendar.Calendar.from_ical(
                    raw.decode(part.get_content_charset() or "utf-8", errors="replace")
                )
            except ValueError as exc:
                logger.warning("unparseable text/calendar part: %s", exc)
                return None
    return None


def parse_mail(raw: bytes) -> ParsedMail:
    msg = email.message_from_bytes(raw)

    senders = _addresses(msg, "From")
    try:
        date = email.utils.parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        date = None

    references = []
    for header in ("In-Reply-To", "References"):
        references.extend((msg.get(header) or "").split())

    parsed = ParsedMail(
        message_id=(msg.get("Message-ID") or "").strip(),
        sender=senders[0] if senders else "",
        recipients=_addresses(msg, "To", "Cc", "Delivered-To", "X-Original-To"),
        subject=msg.get("Subject", ""),
        date=date,
        in_reply_to=references,
        body_text=_body_text(msg),
    )

    calendar = _calendar_part(msg)
    if calendar is not None:
        parsed.calendar = calendar
        method = calendar.get("METHOD")
        parsed.method = str(method).upper() if method else None
        parsed.events = [c for c in calendar.subcomponents if isinstance(c, icalendar.Event)]
    return parsed


def event_uid(event: icalendar.Event) -> str:
    return str(event.get("UID", ""))


def event_sequence(event: icalendar.Event) -> int:
    try:
        return int(event.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        return 0


def has_recurrence_id(event: icalendar.Event) -> bool:
    return event.get("RECURRENCE-ID") is not None


def organizer_address(event: icalendar.Event) -> str:
    organizer = event.get("ORGANIZER")
    if not organizer:
        return ""
    return str(organizer).removeprefix("mailto:").removeprefix("MAILTO:").lower()


def attendee_addresses(event: icalendar.Event) -> list[str]:
    attendees = event.get("ATTENDEE")
    if attendees is None:
        return []
    if not isinstance(attendees, list):
        attendees = [attendees]
    return [str(a).removeprefix("mailto:").removeprefix("MAILTO:").lower() for a in attendees]
