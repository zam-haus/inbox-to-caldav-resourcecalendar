"""Configuration loading and validation (FR-6, FR-10).

Configuration is a TOML file; any string value may be written as "env:NAME"
to read it from the environment (a .env file is loaded first), so secrets
can stay out of the config file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import dotenv


class ConfigError(Exception):
    pass


def _resolve(value: str, context: str) -> str:
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        resolved = os.getenv(name)
        if resolved is None:
            raise ConfigError(f"{context}: environment variable {name!r} is not set")
        return resolved
    return value


def _norm_addr(addr: str) -> str:
    return addr.strip().lower()


@dataclass
class ImapConfig:
    server: str
    user: str
    password: str
    inbox: str = "INBOX"
    # RFC 3501 SEARCH criteria applied in addition to UNSEEN
    filter: str = "ALL"


@dataclass
class SmtpConfig:
    server: str
    user: str
    password: str
    from_address: str
    port: int = 465


@dataclass
class CaldavConfig:
    url: str
    username: str
    password: str


@dataclass
class ResourceConfig:
    email: str
    calendar_url: str
    display_name: str = ""
    organizer_allowlist: list[str] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.email = _norm_addr(self.email)
        self.organizer_allowlist = [_norm_addr(a) for a in self.organizer_allowlist]
        self.approvers = [_norm_addr(a) for a in self.approvers]

    def is_trusted_organizer(self, addr: str) -> bool:
        return _norm_addr(addr) in self.organizer_allowlist

    def is_approver(self, addr: str) -> bool:
        return _norm_addr(addr) in self.approvers


@dataclass
class Config:
    imap: ImapConfig
    smtp: SmtpConfig
    caldav: CaldavConfig
    resources: list[ResourceConfig]
    state_path: Path
    # horizon for expanding recurring events during conflict checks (FR-9)
    conflict_horizon_days: int = 365

    def resource_for(self, addr: str) -> ResourceConfig | None:
        addr = _norm_addr(addr)
        for res in self.resources:
            if res.email == addr:
                return res
        return None


def _section(data: dict, name: str) -> dict:
    try:
        section = data[name]
    except KeyError:
        raise ConfigError(f"missing [{name}] section") from None
    return {k: _resolve(v, f"[{name}] {k}") if isinstance(v, str) else v for k, v in section.items()}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    dotenv.load_dotenv()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    resources_raw = data.get("resources") or []
    if not resources_raw:
        raise ConfigError("at least one [[resources]] entry is required")
    resources = []
    for i, res in enumerate(resources_raw):
        try:
            resources.append(ResourceConfig(**res))
        except TypeError as exc:
            raise ConfigError(f"[[resources]] entry {i}: {exc}") from exc
        if not resources[-1].approvers:
            raise ConfigError(f"[[resources]] entry {i} ({resources[-1].email}): approvers must not be empty")

    emails = [r.email for r in resources]
    if len(set(emails)) != len(emails):
        raise ConfigError("duplicate resource email addresses in config")

    general = data.get("general", {})
    state_path = Path(general.get("state_path", path.parent / "state.sqlite3"))

    try:
        return Config(
            imap=ImapConfig(**_section(data, "imap")),
            smtp=SmtpConfig(**_section(data, "smtp")),
            caldav=CaldavConfig(**_section(data, "caldav")),
            resources=resources,
            state_path=state_path,
            conflict_horizon_days=int(general.get("conflict_horizon_days", 365)),
        )
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc
