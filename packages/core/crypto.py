from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet

# KMS-style abstraction. For local dev this is a Fernet key from env; swap the
# body for a real KMS in production. Credentials are NEVER stored in plaintext —
# connectors seal tokens with seal() and read them back with unseal().


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "FERNET_KEY not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def seal(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def unseal(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
