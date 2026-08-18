from __future__ import annotations


class MarkvectorError(Exception):
    """Base for everything this client raises. Catch this to catch them all."""


class AuthError(MarkvectorError):
    """The key is missing, wrong, revoked, or lacks the scope for this call."""


class NotFound(MarkvectorError):
    """No such collection, document or trace in this workspace."""


class InvalidRequest(MarkvectorError):
    """The server rejected the request. The message is the server's own."""


class RateLimited(MarkvectorError):
    """Too many requests, or too many at once. Raised after retries are spent.

    Its own type rather than a generic failure because the caller can do
    something specific about it: `retry_after` is the server's own answer, in
    seconds, to "when should I come back?" — already waited through
    automatically for GETs, so seeing this means the wait exceeded the client's
    retry budget rather than that the limit was momentary.

        try:
            answers = docs.answer(question)
        except RateLimited as limit:
            schedule_again_in(limit.retry_after)
    """

    def __init__(self, message: str, retry_after: int = 1, limit: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        # The burst capacity that was exhausted, when the server reported one.
        self.limit = limit


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
    "RateLimited",
    "Unavailable",
]
