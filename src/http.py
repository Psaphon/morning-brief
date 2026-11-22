"""Shared outbound HTTP client: one User-Agent naming the tool and an operator contact.

Every module builds its ``httpx.AsyncClient`` through :func:`make_client` so all requests
carry the same header: ``MorningBrief/<version> (contact: <email or url>)``. The contact
comes from ``CONTACT_EMAIL`` (preferred) or ``CONTACT_URL``. With neither set the header is
contact-less and one WARNING is logged per process. The address itself is never logged.
"""

from __future__ import annotations

import logging
from importlib import metadata

import httpx

from .config import contact_from_env

logger = logging.getLogger(__name__)

TOOL_NAME = "MorningBrief"

_warned = False


def tool_version() -> str:
    try:
        return metadata.version("morning-brief")
    except metadata.PackageNotFoundError:
        return "0"


def build_user_agent(contact_email: str = "", contact_url: str = "") -> str:
    """Tool name, version and contact (email wins over URL); contact-less if neither."""
    global _warned
    base = f"{TOOL_NAME}/{tool_version()}"
    contact = contact_email.strip() or contact_url.strip()
    if contact:
        return f"{base} (+{contact})" if "://" in contact else f"{base} (contact: {contact})"
    if not _warned:
        _warned = True
        logger.warning("CONTACT_EMAIL and CONTACT_URL are unset; sending a contact-less User-Agent")
    return base


def make_client(
    contact_email: str | None = None, contact_url: str | None = None, **kwargs
) -> httpx.AsyncClient:
    """The one place an AsyncClient is built. Contact defaults to the environment."""
    if contact_email is None and contact_url is None:
        contact_email, contact_url = contact_from_env()
    headers = {
        **kwargs.pop("headers", {}),
        "User-Agent": build_user_agent(contact_email or "", contact_url or ""),
    }
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(headers=headers, **kwargs)
