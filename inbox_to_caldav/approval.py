"""Approval workflow: forward pending bookings, match ACCEPT/REJECT replies (FR-6)."""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .imip import ParsedMail

logger = logging.getLogger(__name__)

_DECISION_RE = re.compile(r"^\s*(ACCEPT|REJECT)\b", re.IGNORECASE | re.MULTILINE)
_TOKEN_RE = re.compile(r"\[booking:([0-9a-f-]{36})\]")


@dataclass
class PendingBooking:
    token: str
    resource_email: str
    uid: str
    sequence: int
    forward_message_id: str
    organizer: str


class ApprovalStore:
    """Persists the correlation between forwarded approval mails and bookings."""

    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending (
                token TEXT PRIMARY KEY,
                resource_email TEXT NOT NULL,
                uid TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                forward_message_id TEXT NOT NULL,
                organizer TEXT NOT NULL,
                created TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def new_token() -> str:
        return str(uuid.uuid4())

    def add(self, booking: PendingBooking) -> None:
        # a new request/update supersedes any older pending entry for the UID (FR-8)
        self._db.execute(
            "DELETE FROM pending WHERE resource_email = ? AND uid = ?",
            (booking.resource_email, booking.uid),
        )
        self._db.execute(
            "INSERT INTO pending (token, resource_email, uid, sequence, forward_message_id, organizer)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                booking.token,
                booking.resource_email,
                booking.uid,
                booking.sequence,
                booking.forward_message_id,
                booking.organizer,
            ),
        )
        self._db.commit()

    def _row_to_booking(self, row) -> PendingBooking:
        return PendingBooking(*row)

    def find_by_message_ids(self, message_ids: list[str]) -> PendingBooking | None:
        for message_id in message_ids:
            row = self._db.execute(
                "SELECT token, resource_email, uid, sequence, forward_message_id, organizer"
                " FROM pending WHERE forward_message_id = ?",
                (message_id,),
            ).fetchone()
            if row:
                return self._row_to_booking(row)
        return None

    def find_by_token(self, token: str) -> PendingBooking | None:
        row = self._db.execute(
            "SELECT token, resource_email, uid, sequence, forward_message_id, organizer"
            " FROM pending WHERE token = ?",
            (token,),
        ).fetchone()
        return self._row_to_booking(row) if row else None

    def resolve(self, token: str) -> None:
        self._db.execute("DELETE FROM pending WHERE token = ?", (token,))
        self._db.commit()


@dataclass
class ApprovalDecision:
    booking: PendingBooking
    accepted: bool


def parse_reply(store: ApprovalStore, mail: ParsedMail) -> ApprovalDecision | None:
    """Match a plain mail against pending bookings; None if it is not a valid decision.

    Threading (In-Reply-To/References) is checked first, a [booking:<token>]
    marker in subject or body is the fallback (FR-6).
    """
    booking = store.find_by_message_ids(mail.in_reply_to)
    if booking is None:
        match = _TOKEN_RE.search(mail.subject + "\n" + mail.body_text)
        if match:
            booking = store.find_by_token(match.group(1))
    if booking is None:
        return None

    decision = _DECISION_RE.search(mail.body_text)
    if decision is None:
        logger.info("reply for booking %s contains no ACCEPT/REJECT; ignoring", booking.uid)
        return None
    return ApprovalDecision(booking=booking, accepted=decision.group(1).upper() == "ACCEPT")
