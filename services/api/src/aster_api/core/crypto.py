"""AES-256-GCM encryption for chat content at rest.

The message id is bound as associated data, so a ciphertext copied onto another row fails to decrypt.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class ContentCipher:
    def __init__(self, ring: dict[str, bytes], active_key_id: str) -> None:
        self._ring = {kid: AESGCM(key) for kid, key in ring.items()}
        self.active_key_id = active_key_id

    def encrypt(self, plaintext: str, row_id: uuid.UUID) -> tuple[bytes, str]:
        nonce = os.urandom(_NONCE_LEN)
        ct = self._ring[self.active_key_id].encrypt(nonce, plaintext.encode("utf-8"), row_id.bytes)
        return nonce + ct, self.active_key_id

    def decrypt(self, blob: bytes, key_id: str, row_id: uuid.UUID) -> str:
        aead = self._ring[key_id]
        return aead.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], row_id.bytes).decode("utf-8")


def hash_token(token: str, pepper: str) -> bytes:
    return hmac.new(pepper.encode(), token.encode(), hashlib.sha256).digest()


def subject_hash(user_id: uuid.UUID, pepper: str) -> str:
    return hmac.new(pepper.encode(), b"ledger:" + user_id.bytes, hashlib.sha256).hexdigest()
