"""Outgoing mail: iMIP REPLYs to organizers and approval forwards (FR-7)."""

from __future__ import annotations

import email.utils
import logging
import smtplib
from email.message import EmailMessage

import icalendar

from . import imip
from .config import ResourceConfig, SmtpConfig

logger = logging.getLogger(__name__)


def _build_reply_calendar(event: icalendar.Event, resource: ResourceConfig, partstat: str) -> icalendar.Calendar:
    """iTIP REPLY (RFC 5546 §3.2.3): the resource answers as ATTENDEE."""
    reply = icalendar.Event()
    # mirror the identifying and time/recurrence properties of the request, so
    # clients match the reply against the original event instead of striking
    # through "changed" details
    for name in (
        "UID",
        "SEQUENCE",
        "DTSTART",
        "DTEND",
        "DURATION",
        "RRULE",
        "RDATE",
        "EXDATE",
        "RECURRENCE-ID",
        "SUMMARY",
    ):
        value = event.get(name)
        if value is not None:
            reply.add(name, value, encode=False)
    organizer = event.get("ORGANIZER")
    if organizer is not None:
        reply.add("ORGANIZER", organizer, encode=False)
    reply.add("DTSTAMP", icalendar.prop.vDatetime(email.utils.localtime()))
    attendee = icalendar.vCalAddress("MAILTO:" + resource.email)
    attendee.params["PARTSTAT"] = partstat
    attendee.params["CN"] = resource.display_name or resource.email
    reply.add("ATTENDEE", attendee, encode=False)

    cal = icalendar.Calendar()
    cal.add("PRODID", "-//inbox-to-caldav-resourcecalendar//EN")
    cal.add("VERSION", "2.0")
    cal.add("METHOD", "REPLY")
    cal.add_component(reply)
    cal.add_missing_timezones()
    return cal


_MAX_LISTED_OCCURRENCES = 15
_OCCURRENCE_HORIZON_DAYS = 400

_WEEKDAYS = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_FREQ_UNITS = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= abs(n) % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(abs(n) % 10, "th")
    return f"{n}{suffix}"


def _format_byday(entry) -> str:
    entry = str(entry)
    day = _WEEKDAYS.get(entry[-2:], entry)
    if len(entry) > 2:
        n = int(entry[:-2])
        return f"the last {day}" if n == -1 else f"the {_ordinal(n)} {day}"
    return day


def _humanize_rrule(rrule: icalendar.vRecur) -> str:
    """Render common RRULEs as English; fall back to the raw rule."""
    raw = rrule.to_ical().decode()
    try:
        freq = rrule["FREQ"][0].upper()
        unit = _FREQ_UNITS[freq]
        interval = int(rrule.get("INTERVAL", [1])[0])
        text = f"every {unit}" if interval == 1 else f"every {interval} {unit}s"

        if "BYDAY" in rrule:
            days = ", ".join(_format_byday(d) for d in rrule["BYDAY"])
            text += f" on {days}"
        if "BYMONTHDAY" in rrule:
            days = ", ".join(_ordinal(int(d)) for d in rrule["BYMONTHDAY"])
            text += f" on the {days}"
        if "BYMONTH" in rrule:
            months = ", ".join(_MONTHS[int(m) - 1] for m in rrule["BYMONTH"])
            text += f" in {months}"
        if "COUNT" in rrule:
            count = int(rrule["COUNT"][0])
            text += f", {count} times"
        if "UNTIL" in rrule:
            text += f", until {_format_dt(rrule['UNTIL'][0])}"

        handled = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL", "WKST"}
        leftover = set(map(str.upper, rrule)) - handled
        if leftover:
            return f"{text} ({raw})"
        return text
    except (KeyError, ValueError, IndexError, TypeError):
        return raw


def _format_dt(value) -> str:
    from datetime import datetime

    if isinstance(value, datetime):
        return value.strftime("%a %Y-%m-%d %H:%M %Z").strip()
    return value.strftime("%a %Y-%m-%d (all day)")


def _event_summary_text(event: icalendar.Event) -> str:
    """Human-readable summary of the event: metadata, RRULE, occurrence dates."""
    from datetime import datetime, timedelta, timezone

    from .caldav_store import expand

    lines = []

    def add(label: str, prop: str, formatter=str):
        value = event.get(prop)
        if value is not None:
            lines.append(f"{label}: {formatter(value)}")

    add("Summary", "SUMMARY")
    lines.append(f"Organizer: {imip.organizer_address(event)}")
    add("Start", "DTSTART", lambda v: _format_dt(v.dt))
    add("End", "DTEND", lambda v: _format_dt(v.dt))
    add("Location", "LOCATION")
    add("Description", "DESCRIPTION")
    add("Repeats", "RRULE", _humanize_rrule)
    add("Extra dates", "RDATE", lambda v: v.to_ical().decode())
    add("Excluded dates", "EXDATE", lambda v: v.to_ical().decode())
    lines.append(f"UID: {imip.event_uid(event)}")

    if event.get("RRULE") is not None or event.get("RDATE") is not None:
        now = datetime.now(timezone.utc)
        try:
            occurrences = expand(event, now - timedelta(days=1), now + timedelta(days=_OCCURRENCE_HORIZON_DAYS))
        except Exception as exc:  # summary must never break the forward
            logger.warning("could not expand occurrences for approval mail: %s", exc)
            occurrences = []
        if occurrences:
            lines.append("")
            lines.append("Occurrences:")
            for occ in occurrences[:_MAX_LISTED_OCCURRENCES]:
                lines.append(f"  - {_format_dt(occ.start)} to {_format_dt(occ.end)}")
            if len(occurrences) > _MAX_LISTED_OCCURRENCES:
                lines.append(f"  … and {len(occurrences) - _MAX_LISTED_OCCURRENCES} more within the next year")

    return "\n".join(lines) + "\n"


class Mailer:
    def __init__(self, config: SmtpConfig, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run

    def _send(self, msg: EmailMessage) -> None:
        if "From" not in msg:
            msg["From"] = self._config.from_address
        msg["Date"] = email.utils.formatdate(localtime=True)
        if "Message-ID" not in msg:
            msg["Message-ID"] = email.utils.make_msgid()
        if self._dry_run:
            logger.info("dry-run: would send %r to %s", msg["Subject"], msg["To"])
            return
        with smtplib.SMTP_SSL(self._config.server, self._config.port) as smtp:
            smtp.login(self._config.user, self._config.password)
            smtp.send_message(msg)
        logger.info("sent %r to %s", msg["Subject"], msg["To"])

    def send_imip_reply(
        self,
        event: icalendar.Event,
        resource: ResourceConfig,
        organizer: str,
        partstat: str,  # ACCEPTED | TENTATIVE | DECLINED
        explanation: str = "",
    ) -> None:
        if not organizer:
            logger.warning("event %s has no organizer address; cannot send REPLY", imip.event_uid(event))
            return
        summary = str(event.get("SUMMARY", ""))
        subject = {
            "ACCEPTED": f"Accepted: {summary}",
            "TENTATIVE": f"Tentatively accepted (pending approval): {summary}",
            "DECLINED": f"Declined: {summary}",
        }[partstat]

        msg = EmailMessage()
        # replies come from the room itself, so clients associate them with the ATTENDEE
        msg["From"] = resource.email
        msg["To"] = organizer
        msg["Subject"] = subject
        body = explanation or f"The resource {resource.email} has responded: {partstat}."
        msg.set_content(body)
        cal = _build_reply_calendar(event, resource, partstat)
        msg.add_attachment(
            cal.to_ical(),
            maintype="text",
            subtype="calendar",
            params={"method": "REPLY", "charset": "utf-8"},
        )
        self._send(msg)

    def send_approval_forward(
        self,
        raw_mail: bytes,
        event: icalendar.Event,
        resource: ResourceConfig,
        token: str,
        message_id: str,
    ) -> None:
        """Forward a pending request to the approvers.

        The Message-ID is generated by the caller and persisted before sending,
        so a crash between the two cannot orphan the pending booking.
        """
        summary = str(event.get("SUMMARY", ""))
        msg = EmailMessage()
        msg["Message-ID"] = message_id
        msg["From"] = resource.email
        msg["To"] = ", ".join(resource.approvers)
        msg["Subject"] = f"Approval needed: {summary} [booking:{token}]"
        msg.set_content(
            f"A booking request for {resource.email} needs approval.\n"
            f"\n"
            f"{_event_summary_text(event)}"
            f"\n"
            f'Reply to this mail with a first line of "ACCEPT" or "REJECT".\n'
            f"Reference: [booking:{token}]\n"
        )
        msg.add_attachment(
            raw_mail,
            maintype="message",
            subtype="rfc822",
            filename="original-request.eml",
        )
        self._send(msg)
