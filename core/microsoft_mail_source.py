"""Dependency-light source-to-pool routing for Outlook and Hotmail mailboxes."""

from __future__ import annotations

ACCOUNT_TYPE_MICROSOFT_OAUTH = "microsoft_oauth"
ACCOUNT_TYPE_MAILAPI_URL = "mailapi_url"

MAIL_IMPORT_SOURCE_APPLEMAIL = "applemail"
MAIL_IMPORT_SOURCE_OUTLOOK = "outlook"
MAIL_IMPORT_SOURCE_HOTMAIL = "hotmail"
MAIL_IMPORT_SOURCE_MAILAPI = "mailapi"

_SOURCE_POOL_ACCOUNT_TYPES = {
    MAIL_IMPORT_SOURCE_OUTLOOK: ACCOUNT_TYPE_MICROSOFT_OAUTH,
    MAIL_IMPORT_SOURCE_HOTMAIL: ACCOUNT_TYPE_MICROSOFT_OAUTH,
    MAIL_IMPORT_SOURCE_MAILAPI: ACCOUNT_TYPE_MAILAPI_URL,
}


def resolve_microsoft_pool_account_type(source: object) -> str:
    """Return the account-type filter; empty preserves the legacy mixed pool."""
    normalized = str(source or "").strip().lower()
    if normalized == "microsoft":
        normalized = MAIL_IMPORT_SOURCE_OUTLOOK
    return _SOURCE_POOL_ACCOUNT_TYPES.get(normalized, "")
