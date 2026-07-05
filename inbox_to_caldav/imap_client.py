"""Fetch unseen mail from the configured IMAP inbox (FR-1).

Unseen messages are fetched without setting flags; the pipeline marks a
message \\Seen only after it was fully processed, so failed messages are
retried on the next run.
"""

from __future__ import annotations

import imaplib
import logging
from dataclasses import dataclass

from icecream import ic

from .config import ImapConfig

logger = logging.getLogger(__name__)


@dataclass
class FetchedMail:
    imap_id: bytes
    raw: bytes


class ImapClient:
    """Holds one IMAP connection, kept open across polls."""

    def __init__(self, config: ImapConfig):
        self._config = config
        self._conn: imaplib.IMAP4_SSL | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> "ImapClient":
        self._conn = imaplib.IMAP4_SSL(self._config.server)
        self._conn.login(self._config.user, self._config.password)
        result, _ = self._conn.select(self._config.inbox)
        if result != "OK":
            raise ConnectionError(f"cannot select mailbox {self._config.inbox!r}")
        logger.info("connected to %s, mailbox %s", self._config.server, self._config.inbox)
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            self._conn = None

    def check_alive(self) -> None:
        """NOOP keep-alive; raises if the connection has gone away."""
        result, _ = self._conn.noop()
        if result != "OK":
            raise ConnectionError("IMAP NOOP failed")

    def __enter__(self) -> "ImapClient":
        return self.connect()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def fetch_unseen(self) -> list[FetchedMail]:
        criteria = f"(UNSEEN {self._config.filter})" if self._config.filter != "ALL" else "(UNSEEN)"
        result, data = self._conn.search(None, criteria)
        if result != "OK":
            raise ConnectionError(f"IMAP search failed: {result}")
        mails = []
        for imap_id in data[0].split():
            # BODY.PEEK keeps the message unseen until we explicitly mark it
            result, msg_data = self._conn.fetch(imap_id, "(BODY.PEEK[])")
            # msg_data mixes (b'<id> (BODY[] {n}', <raw>) tuples with bare
            # framing bytes like b')'; only the tuples carry the message
            raw = next(
                (part[1] for part in msg_data or [] if isinstance(part, tuple) and len(part) >= 2),
                None,
            )
            if result != "OK" or not isinstance(raw, bytes):
                logger.warning("could not fetch message %s", imap_id)
                continue
            mails.append(FetchedMail(imap_id=imap_id, raw=raw))
        return mails

    def mark_processed(self, mail: FetchedMail) -> None:
        self._conn.store(mail.imap_id, "+FLAGS", "\\Seen")
