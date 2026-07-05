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
    for name in ("UID", "SEQUENCE", "DTSTART", "DTEND", "RECURRENCE-ID", "SUMMARY"):
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
    return cal


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
    ) -> str:
        """Forward a pending request to the approvers; returns the forward's Message-ID."""
        message_id = email.utils.make_msgid()
        summary = str(event.get("SUMMARY", ""))
        msg = EmailMessage()
        msg["Message-ID"] = message_id
        msg["From"] = resource.email
        msg["To"] = ", ".join(resource.approvers)
        msg["Subject"] = f"Approval needed: {summary} [booking:{token}]"
        msg.set_content(
            f"A booking request for {resource.email} needs approval.\n"
            f"\n"
            f"Summary: {summary}\n"
            f"Organizer: {imip.organizer_address(event)}\n"
            f"UID: {imip.event_uid(event)}\n"
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
        return message_id
