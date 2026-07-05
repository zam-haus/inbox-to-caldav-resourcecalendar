"""CalDAV access for the resource calendars (FR-3, FR-4, FR-9, FR-10)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import caldav
import icalendar
import recurring_ical_events

from . import imip
from .config import CaldavConfig, ResourceConfig
from .policy import ExistingState

logger = logging.getLogger(__name__)

# marks bookings that went through the approval flow (FR-8)
APPROVAL_PROP = "X-INBOX2CALDAV-APPROVAL"


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


@dataclass
class Occurrence:
    start: datetime
    end: datetime

    def overlaps(self, other: "Occurrence") -> bool:
        return self.start < other.end and other.start < self.end


def expand(event: icalendar.Event, horizon_start: datetime, horizon_end: datetime) -> list[Occurrence]:
    cal = icalendar.Calendar()
    cal.add_component(event.copy())
    occurrences = []
    for occ in recurring_ical_events.of(cal).between(horizon_start, horizon_end):
        start = occ.get("DTSTART")
        end = occ.get("DTEND") or occ.get("DTSTART")
        if start is None:
            continue
        occurrences.append(Occurrence(_as_datetime(start.dt), _as_datetime(end.dt)))
    return occurrences


class CaldavStore:
    def __init__(self, config: CaldavConfig, resource: ResourceConfig):
        self._client = caldav.DAVClient(
            url=config.url, username=config.username, password=config.password
        )
        # calendars are addressed by URL, never by display name (FR-10)
        self._calendar = self._client.calendar(url=resource.calendar_url)
        self._resource = resource

    def _find_object(self, uid: str) -> caldav.CalendarObjectResource | None:
        try:
            return self._calendar.event_by_uid(uid)
        except caldav.lib.error.NotFoundError:
            return None

    @staticmethod
    def _main_vevent(obj: caldav.CalendarObjectResource) -> icalendar.Event | None:
        for comp in obj.icalendar_instance.subcomponents:
            if isinstance(comp, icalendar.Event) and comp.get("RECURRENCE-ID") is None:
                return comp
        return None

    def get_event(self, uid: str) -> icalendar.Event | None:
        obj = self._find_object(uid)
        return self._main_vevent(obj) if obj is not None else None

    def existing_state(self, uid: str) -> ExistingState:
        obj = self._find_object(uid)
        if obj is None:
            return ExistingState(exists=False)
        vevent = self._main_vevent(obj)
        if vevent is None:
            return ExistingState(exists=False)
        return ExistingState(
            exists=True,
            sequence=imip.event_sequence(vevent),
            was_auto_accepted=str(vevent.get(APPROVAL_PROP, "NONE")) != "REQUIRED",
        )

    def has_conflict(self, event: icalendar.Event, horizon_days: int) -> bool:
        """True if any occurrence of `event` overlaps a non-cancelled booking (FR-9)."""
        now = datetime.now(timezone.utc)
        horizon_start = now - timedelta(days=1)
        horizon_end = now + timedelta(days=horizon_days)
        new_occurrences = expand(event, horizon_start, horizon_end)
        if not new_occurrences:
            return False
        uid = imip.event_uid(event)

        first = min(o.start for o in new_occurrences)
        last = max(o.end for o in new_occurrences)
        try:
            existing = self._calendar.search(start=first, end=last, event=True, expand=False)
        except caldav.lib.error.DAVError as exc:
            logger.error("conflict search failed, treating as conflict: %s", exc)
            return True

        for obj in existing:
            for comp in obj.icalendar_instance.subcomponents:
                if not isinstance(comp, icalendar.Event):
                    continue
                if imip.event_uid(comp) == uid:
                    continue  # update of the same booking never conflicts with itself
                if str(comp.get("STATUS", "")) == "CANCELLED":
                    continue
                for occ in expand(comp, horizon_start, horizon_end):
                    if any(occ.overlaps(new) for new in new_occurrences):
                        return True
        return False

    def _sanitized_calendar(
        self,
        event: icalendar.Event,
        status: str,
        approval_required: bool,
        timezones: list[icalendar.Timezone],
    ) -> icalendar.Calendar:
        # Privacy: only expose what viewers of the resource calendar need —
        # who booked, what it is called, when it happens, and how to join.
        # Attendee lists, descriptions, locations etc. are dropped.
        kept_properties = (
            "UID",
            "SEQUENCE",
            "DTSTAMP",
            "DTSTART",
            "DTEND",
            "DURATION",
            "RRULE",
            "RDATE",
            "EXDATE",
            "SUMMARY",
            "ORGANIZER",
            "CONFERENCE",
            "X-GOOGLE-CONFERENCE",  # Google Meet link, so the room can join
        )
        comp = icalendar.Event()
        for name in kept_properties:
            value = event.get(name)
            if value is not None:
                comp.add(name, value, encode=False)
        comp["STATUS"] = status
        # cancelled events must not block free/busy; everything else does (FR-9)
        comp["TRANSP"] = "TRANSPARENT" if status == "CANCELLED" else "OPAQUE"
        if status == "CANCELLED" and comp.get("EXDATE") is not None:
            # full-series cancels carry stale EXDATEs some servers choke on
            del comp["EXDATE"]
        if approval_required:
            comp[APPROVAL_PROP] = "REQUIRED"
        cal = icalendar.Calendar()
        cal.add("PRODID", "-//inbox-to-caldav-resourcecalendar//EN")
        cal.add("VERSION", "2.0")
        for tz in timezones:
            cal.add_component(tz)
        cal.add_component(comp)
        return cal

    def upsert(
        self,
        event: icalendar.Event,
        status: str,
        approval_required: bool = False,
        timezones: list[icalendar.Timezone] | None = None,
    ) -> None:
        """Create or overwrite the booking with the given STATUS (never deletes, FR-4)."""
        uid = imip.event_uid(event)
        cal = self._sanitized_calendar(event, status, approval_required, timezones or [])
        existing = self._find_object(uid)
        if existing is not None:
            existing.data = cal.to_ical().decode()
            existing.save(increase_seqno=False)
            logger.info("updated event %s with status %s", uid, status)
        else:
            new = self._calendar.add_event(ical=cal)
            new.save(increase_seqno=False)
            logger.info("created event %s with status %s", uid, status)

    # properties a same-SEQUENCE update may change; times, recurrence,
    # UID/SEQUENCE and STATUS are deliberately kept as stored
    METADATA_PROPS = ("SUMMARY", "ORGANIZER", "CONFERENCE", "X-GOOGLE-CONFERENCE", "DTSTAMP")

    def update_metadata(self, event: icalendar.Event) -> bool:
        """Apply only non-time/non-recurrence properties of `event` to the stored booking."""
        uid = imip.event_uid(event)
        obj = self._find_object(uid)
        vevent = self._main_vevent(obj) if obj is not None else None
        if vevent is None:
            return False
        for name in self.METADATA_PROPS:
            value = event.get(name)
            if value is None:
                continue
            if vevent.get(name) is not None:
                del vevent[name]
            vevent.add(name, value, encode=False)
        obj.save(increase_seqno=False)
        logger.info("updated metadata of event %s", uid)
        return True

    def set_status(self, uid: str, status: str) -> bool:
        """Change only the STATUS of an existing booking (approval decisions, cancels)."""
        obj = self._find_object(uid)
        if obj is None:
            return False
        vevent = self._main_vevent(obj)
        if vevent is None:
            return False
        vevent["STATUS"] = status
        vevent["TRANSP"] = "TRANSPARENT" if status == "CANCELLED" else "OPAQUE"
        obj.save(increase_seqno=False)
        logger.info("set event %s to status %s", uid, status)
        return True
