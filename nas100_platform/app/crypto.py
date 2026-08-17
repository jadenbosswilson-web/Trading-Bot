"""
Encryption for stored broker credentials.

Uses Fernet (symmetric, authenticated encryption from the `cryptography`
library) with a single master key from CREDENTIAL_ENCRYPTION_KEY. This
is a reasonable baseline for a v1 product — it means broker credentials
are never stored as plaintext in the database, and a database leak
alone doesn't hand over anyone's broker login.

It is NOT a substitute for proper secret management at real scale. Before
this handles meaningful numbers of real users/real money, consider:
  - A managed KMS (AWS KMS, GCP KMS, HashiCorp Vault) instead of a raw
    env var, so the key itself is never in plaintext anywhere you can
    read it directly.
  - Per-user encryption keys (envelope encryption) so a single key
    compromise doesn't expose every user's credentials at once.
  - Key rotation support (there is none here — rotating
    CREDENTIAL_ENCRYPTION_KEY today would make all existing stored
    credentials undecryptable; you'd need a migration that
    decrypts-with-old/re-encrypts-with-new for every row).
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

import config

_fernet = Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError(
            "Could not decrypt stored credential — CREDENTIAL_ENCRYPTION_KEY may have "
            "changed since this was saved, or the data is corrupted."
        )
