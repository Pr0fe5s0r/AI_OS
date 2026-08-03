from __future__ import annotations


class MarkvectorError(Exception):
    """Base for everything this client raises. Catch this to catch them all."""


class AuthError(MarkvectorError):
    """The key is missing, wrong, revoked, or lacks the scope for this call."""


class NotFound(MarkvectorError):
    """No such collection, document or trace in this workspace."""


class InvalidRequest(MarkvectorError):
    """The server rejected the request. The message is the server's own."""


class Unavailable(MarkvectorError):
    """The service could not be reached, or failed after retries."""


class IndexingTimeout(MarkvectorError):
    """`wait=True` gave up before the document became searchable.

    Deliberately its own error rather than a silent return: ingestion is
    asynchronous, and code that assumes otherwise is the single most common way
    to write a test that passes locally and flakes in CI. Catch this to retry
    or to raise your own timeout.
    """


__all__ = [
    "AuthError",
    "IndexingTimeout",
    "InvalidRequest",
    "MarkvectorError",
    "NotFound",
    "Unavailable",
]
