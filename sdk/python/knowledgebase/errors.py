from __future__ import annotations


class KnowledgeBaseError(Exception):
    """Base for everything this client raises."""


class AuthError(KnowledgeBaseError):
    """The key is missing, wrong, revoked, or lacks the scope for this call."""


class NotFound(KnowledgeBaseError):
    """No such collection, item or trace in this workspace."""


class InvalidRequest(KnowledgeBaseError):
    """The server rejected the request. The message is the server's own."""


class Unavailable(KnowledgeBaseError):
    """The service could not be reached, or failed after retries."""


class IndexingTimeout(KnowledgeBaseError):
    """`wait=True` gave up before the document became searchable.

    Deliberately its own error rather than a silent return: ingestion is
    asynchronous, and code that assumed otherwise is the single most common
    way to write a test that passes locally and fails in CI.
    """
