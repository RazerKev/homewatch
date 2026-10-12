"""Per-home API keys.

Format: hwk_<8 hex>_<48 hex>, e.g. hwk_3f9a1c2b_9e0d...  (the secret part is 192 random bits)

Only a SHA-256 hash of the key is stored. A fast hash is the right tool here: slow
password hashes (bcrypt, argon2) exist to make guessing low-entropy human passwords
expensive, but a 192-bit random key can't be guessed, so the hash only needs to be
one-way. Lookup is by hash (WHERE api_key_hash = ...), so the raw key is never compared
byte-by-byte, and an attacker can't steer a timing side channel toward a valid hash.

The fixed "hwk_" prefix makes a leaked key easy to recognise and to catch with secret
scanning (GitHub push protection supports custom patterns).
"""

import hashlib
import re
import secrets

_KEY_RE = re.compile(r"hwk_[0-9a-f]{8}_[0-9a-f]{48}")


def generate_api_key() -> str:
    return f"hwk_{secrets.token_hex(4)}_{secrets.token_hex(24)}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def api_key_hint(key: str) -> str:
    """First 12 characters ("hwk_3f9a1c2b"): enough to tell keys apart, useless to an attacker."""
    return key[:12]


def looks_like_api_key(value: str) -> bool:
    return _KEY_RE.fullmatch(value) is not None
