"""Pure booking-policy decisions (FR-5, FR-6, FR-8, FR-9). No I/O here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

import icalendar

from . import imip
from .config import ResourceConfig


class Action(Enum):
    ACCEPT = auto()  # store CONFIRMED, reply ACCEPTED
    TENTATIVE = auto()  # store TENTATIVE, reply TENTATIVE, forward to approvers
    DECLINE = auto()  # do not store, reply DECLINED with reason
    CANCEL = auto()  # store CANCELLED
    UPDATE_METADATA = auto()  # same SEQUENCE: apply only non-time/non-recurrence properties
    IGNORE = auto()  # drop silently


@dataclass
class Decision:
    action: Action
    reason: str = ""


def _norm(addr: str) -> str:
    return (addr or "").strip().lower()


@dataclass
class ExistingState:
    """State of the event with the same UID already in the resource calendar."""

    exists: bool = False
    sequence: int = 0
    was_auto_accepted: bool = True  # False if the stored booking went through approval
    # authenticated sender that created the booking; empty for legacy events
    owner: str = ""


def _is_owner(sender: str, resource: ResourceConfig, existing: ExistingState) -> bool:
    """Authorize an update/cancel of an existing booking.

    The UID and SEQUENCE of a booking are published in the (viewable) resource
    calendar, so they are not secrets. Modifying an existing booking is allowed
    only for the authenticated sender that created it (mail `From`, validated by
    the mail server per SR-1) or for an approver. Legacy bookings without a
    recorded owner fall back to permissive behavior only for approvers.
    """
    if resource.is_approver(sender):
        return True
    if not existing.owner:
        return False
    return _norm(sender) == _norm(existing.owner)


def decide_request(
    event: icalendar.Event,
    sender: str,
    resource: ResourceConfig,
    existing: ExistingState,
    has_conflict: bool,
) -> Decision:
    if imip.has_recurrence_id(event):
        return Decision(
            Action.DECLINE,
            "Edits to single occurrences of a series (RECURRENCE-ID) are not supported "
            "by this resource calendar. Please delete the occurrence and book a "
            "separate event instead.",
        )

    if existing.exists:
        if not _is_owner(sender, resource, existing):
            # someone other than the booking's organizer is trying to change it
            return Decision(Action.DECLINE, "Only the organizer of this booking may modify it.")
        sequence = imip.event_sequence(event)
        if sequence < existing.sequence:
            return Decision(Action.IGNORE, "stale SEQUENCE")
        if sequence == existing.sequence:
            # same SEQUENCE means times/recurrence are unchanged; only other
            # information may be updated, and that needs no re-approval
            return Decision(Action.UPDATE_METADATA, "same SEQUENCE; metadata-only update")

    if has_conflict:
        return Decision(Action.DECLINE, "The requested time conflicts with an existing booking.")

    trusted = resource.is_trusted_organizer(sender)
    if not trusted:
        return Decision(Action.TENTATIVE, "organizer not on allowlist; approval required")
    if existing.exists and not existing.was_auto_accepted:
        # FR-8: updates to an approved booking need re-approval
        return Decision(Action.TENTATIVE, "update to an approved booking; re-approval required")
    return Decision(Action.ACCEPT)


def decide_cancel(event: icalendar.Event, sender: str, resource: ResourceConfig, existing: ExistingState) -> Decision:
    if imip.has_recurrence_id(event):
        return Decision(
            Action.DECLINE,
            "Cancelling single occurrences of a series (RECURRENCE-ID) is not supported "
            "by this resource calendar. Please exclude the occurrence from the series instead.",
        )
    if not existing.exists:
        return Decision(Action.IGNORE, "CANCEL for unknown UID")
    if not _is_owner(sender, resource, existing):
        # do not act on a cancel forged by someone who is not the organizer
        return Decision(Action.IGNORE, "CANCEL from non-organizer")
    return Decision(Action.CANCEL)
