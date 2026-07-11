from __future__ import annotations

import hashlib
import hmac

# Generic webhook signature check. The sender (GitHub, Slack, Stripe, ...) signs
# the raw request body with a shared secret; anything unsigned is an attacker
# talking to a public URL. Which header carries the signature and which prefix
# it uses is the caller's (vertical's) knowledge — passed in as data.


def verify_signature(
    secret: str, body: bytes, signature: str | None, prefix: str = "sha256="
) -> bool:
    """Constant-time HMAC-SHA256 check of a webhook body."""
    if not secret or not signature or not signature.startswith(prefix):
        return False
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
