"""Determine which configured resource a message addresses (FR-10)."""

from __future__ import annotations

import logging

from . import imip
from .config import Config, ResourceConfig
from .imip import ParsedMail

logger = logging.getLogger(__name__)


def route(config: Config, mail: ParsedMail) -> ResourceConfig | None:
    """Return the resource a scheduling mail is addressed to, or None.

    Recipient headers are checked first, then the ATTENDEE list of the
    events, since some senders address the resource only there.
    """
    candidates: list[str] = list(mail.recipients)
    for event in mail.events:
        candidates.extend(imip.attendee_addresses(event))

    matches: list[ResourceConfig] = []
    for addr in candidates:
        res = config.resource_for(addr)
        if res is not None and res not in matches:
            matches.append(res)

    if not matches:
        logger.info("mail %s matches no configured resource; skipping", mail.message_id)
        return None
    if len(matches) > 1:
        logger.warning(
            "mail %s addresses multiple resources (%s); using first match %s",
            mail.message_id,
            ", ".join(r.email for r in matches),
            matches[0].email,
        )
    return matches[0]
