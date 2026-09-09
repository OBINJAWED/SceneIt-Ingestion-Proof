"""Quota-ledger identity, deliberately separate from private file ownership."""
import hashlib
import hmac
import os
import unicodedata


def normalize_verified_email(email):
    """Return the stable, conservative identity used only for trial accounting."""
    if not isinstance(email, str):
        raise ValueError("A verified email is required")
    normalized = unicodedata.normalize("NFC", email).strip().casefold()
    if (
        not normalized
        or len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("A verified email is invalid")
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain:
        raise ValueError("A verified email is invalid")
    return f"{local}@{domain}"


def trial_ledger_id(email, secret=None):
    """Derive a non-reversible, deployment-stable ledger ID.

    FIREBASE_TRIAL_HASH_SECRET is intentionally not allowed to fall back to the
    session secret. Rotating it would create new ledgers and reset usage, so it
    is required whenever Firebase is configured and must remain immutable.
    """
    secret = secret if secret is not None else os.environ.get(
        "FIREBASE_TRIAL_HASH_SECRET"
    )
    if not isinstance(secret, str) or len(secret) < 32:
        raise RuntimeError(
            "FIREBASE_TRIAL_HASH_SECRET must contain at least 32 immutable characters"
        )
    digest = hmac.new(
        secret.encode("utf-8"),
        normalize_verified_email(email).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"firebase-email-v1:{digest}"


def email_fingerprint(email):
    """Non-secret comparison fingerprint; never used as a quota identity."""
    return hashlib.sha256(normalize_verified_email(email).encode("utf-8")).hexdigest()


def usage_owner(conn, owner_id):
    """Resolve an app owner to its durable quota ledger, preserving legacy IDs."""
    row = conn.execute(
        "SELECT u.provider,i.trial_ledger_id "
        "FROM sceneit_auth_users u LEFT JOIN sceneit_firebase_identities i "
        "ON i.owner_id=u.id WHERE u.id=%s",
        (owner_id,),
    ).fetchone()
    if not row:
        # Imported records may predate auth and retain a legacy owner ID.
        return owner_id
    if row["provider"] != "firebase":
        return owner_id
    ledger = row["trial_ledger_id"]
    if not ledger:
        raise RuntimeError("Verified Firebase identity has no trial ledger")
    return ledger