"""Orchestrates one processing run over the inbox."""

from __future__ import annotations

import email.utils
import imaplib
import logging
import threading
from datetime import datetime, timezone

import icalendar

from . import imip
from .approval import ApprovalStore, PendingBooking, parse_reply
from .caldav_store import CaldavStore
from .config import Config, ResourceConfig
from .imap_client import FetchedMail, ImapClient
from .imip import ParsedMail
from .policy import Action, decide_cancel, decide_request
from .smtp_out import Mailer

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: Config, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run
        self._mailer = Mailer(config.smtp, dry_run=dry_run)
        self._stores: dict[str, CaldavStore] = {}
        self._approvals = ApprovalStore(config.state_path)
        self._client = ImapClient(config.imap)

    def close(self) -> None:
        self._client.close()
        self._approvals.close()

    def _store_for(self, resource: ResourceConfig) -> CaldavStore:
        if resource.email not in self._stores:
            self._stores[resource.email] = CaldavStore(self._config.caldav, resource)
        return self._stores[resource.email]

    def run(self) -> None:
        """Process the inbox once, reusing the persistent IMAP connection."""
        if not self._client.connected:
            self._client.connect()
        else:
            self._client.check_alive()
        mails = self._client.fetch_unseen()
        # apply in mail order so updates supersede older state correctly
        mails_parsed = []
        for m in mails:
            try:
                mails_parsed.append((m, imip.parse_mail(m.raw)))
            except Exception:
                logger.exception("unparseable mail %s; leaving unseen", m.imap_id)
        mails_parsed.sort(key=lambda pair: pair[1].date or datetime.now(timezone.utc))
        if mails_parsed:
            logger.info("processing %d unseen mails", len(mails_parsed))
        for fetched, parsed in mails_parsed:
            try:
                self._process(fetched, parsed)
            except Exception:
                logger.exception("processing mail %s failed; leaving unseen for retry", parsed.message_id)
                continue
            if not self._dry_run:
                self._client.mark_processed(fetched)

    def run_forever(self, interval: int, stop: threading.Event) -> None:
        """Poll until `stop` is set (signal), keeping the IMAP connection open."""
        while not stop.is_set():
            try:
                self.run()
            except (OSError, imaplib.IMAP4.error, ConnectionError) as exc:
                logger.error("poll failed: %s; reconnecting next round", exc)
                self._client.close()
            stop.wait(interval)

    def _process(self, fetched: FetchedMail, parsed: ParsedMail) -> None:
        if parsed.is_unsupported_scheduling:
            # FR-11: only REQUEST and CANCEL are handled
            logger.info("ignoring iTIP method %s from %s", parsed.method, parsed.sender)
            return
        if parsed.is_scheduling:
            self._process_scheduling(fetched, parsed)
        else:
            self._process_possible_approval_reply(parsed)

    # -- scheduling messages ------------------------------------------------

    def _process_scheduling(self, fetched: FetchedMail, parsed: ParsedMail) -> None:
        from .routing import route

        resource = route(self._config, parsed)
        if resource is None:
            return
        store = self._store_for(resource)
        timezones = [c for c in parsed.calendar.subcomponents if isinstance(c, icalendar.Timezone)]

        for event in parsed.events:
            uid = imip.event_uid(event)
            existing = store.existing_state(uid)
            organizer = imip.organizer_address(event) or parsed.sender

            if parsed.method == "CANCEL":
                decision = decide_cancel(event, parsed.sender, resource, existing)
            else:
                conflict = decision = None
                if not imip.has_recurrence_id(event):
                    conflict = store.has_conflict(event, self._config.conflict_horizon_days)
                decision = decide_request(event, parsed.sender, resource, existing, bool(conflict))

            logger.info(
                "mail %s uid %s: %s (%s)", parsed.message_id, uid, decision.action.name, decision.reason
            )
            if self._dry_run:
                continue

            match decision.action:
                case Action.IGNORE:
                    pass
                case Action.UPDATE_METADATA:
                    store.update_metadata(event)
                    # recover from a crash between upsert and forward: an
                    # approval-required booking must always have a pending entry
                    # matching the stored sequence
                    pending = self._approvals.find_by_uid(resource.email, uid)
                    if not existing.was_auto_accepted and (
                        pending is None or pending.sequence != existing.sequence
                    ):
                        logger.warning("tentative booking %s has no current pending approval; re-forwarding", uid)
                        self._forward_for_approval(fetched, event, resource, organizer)
                case Action.DECLINE:
                    self._mailer.send_imip_reply(event, resource, organizer, "DECLINED", decision.reason)
                case Action.ACCEPT:
                    store.upsert(event, "CONFIRMED", approval_required=False, timezones=timezones, owner=parsed.sender)
                    self._mailer.send_imip_reply(event, resource, organizer, "ACCEPTED")
                case Action.CANCEL:
                    store.upsert(event, "CANCELLED", timezones=timezones, owner=existing.owner or parsed.sender)
                case Action.TENTATIVE:
                    store.upsert(
                        event, "TENTATIVE", approval_required=True, timezones=timezones, owner=parsed.sender
                    )
                    self._forward_for_approval(fetched, event, resource, organizer)
                    self._mailer.send_imip_reply(
                        event,
                        resource,
                        organizer,
                        "TENTATIVE",
                        "Your booking was tentatively accepted and awaits approval.",
                    )

    def _forward_for_approval(self, fetched: FetchedMail, event, resource: ResourceConfig, organizer: str) -> None:
        """Record the pending booking first, then send the forward, so a crash
        in between leaves a recoverable state (the token still matches)."""
        token = ApprovalStore.new_token()
        forward_id = email.utils.make_msgid()
        self._approvals.add(
            PendingBooking(
                token=token,
                resource_email=resource.email,
                uid=imip.event_uid(event),
                sequence=imip.event_sequence(event),
                forward_message_id=forward_id,
                organizer=organizer,
            )
        )
        self._mailer.send_approval_forward(fetched.raw, event, resource, token, forward_id)

    # -- approval replies ---------------------------------------------------

    def _process_possible_approval_reply(self, parsed: ParsedMail) -> None:
        decision = parse_reply(self._approvals, parsed)
        if decision is None:
            return
        booking = decision.booking
        resource = self._config.resource_for(booking.resource_email)
        if resource is None:
            logger.warning("pending booking %s references unconfigured resource", booking.uid)
            return
        if not resource.is_approver(parsed.sender):
            logger.warning("approval reply from non-approver %s for %s; ignoring", parsed.sender, booking.uid)
            return

        store = self._store_for(resource)
        event = store.get_event(booking.uid)
        if event is None:
            logger.warning("pending booking %s no longer in calendar; resolving", booking.uid)
            self._approvals.resolve(booking.token)
            return
        if imip.event_sequence(event) != booking.sequence:
            # superseded by a newer update (FR-8); the newer pending entry governs
            logger.info("approval reply for outdated sequence of %s; ignoring", booking.uid)
            return

        logger.info(
            "approver %s %s booking %s", parsed.sender, "accepted" if decision.accepted else "rejected", booking.uid
        )
        if self._dry_run:
            return
        if decision.accepted:
            store.set_status(booking.uid, "CONFIRMED")
            self._mailer.send_imip_reply(event, resource, booking.organizer, "ACCEPTED")
        else:
            store.set_status(booking.uid, "CANCELLED")
            self._mailer.send_imip_reply(
                event, resource, booking.organizer, "DECLINED", "Your booking request was rejected."
            )
        self._approvals.resolve(booking.token)
