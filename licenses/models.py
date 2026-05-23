"""License keys + the events that affect them.

Storage rule: never the cleartext key. The customer receives the key
exactly once (in the /register response); the control center keeps only
its sha256 hash plus the first 8 chars of the cleartext for support
lookup. If the customer loses their key, they get a new one — there is
no recovery of the old.
"""
import hashlib
import secrets

from django.db import models


KEY_LENGTH_BYTES = 48  # → 64 chars urlsafe base64


def _new_key_cleartext() -> str:
    """Return a fresh license key string. Caller stores the sha256 and
    returns the cleartext to the customer exactly once."""
    return secrets.token_urlsafe(KEY_LENGTH_BYTES)


def _hash_key(cleartext: str) -> str:
    return hashlib.sha256(cleartext.encode('utf-8')).hexdigest()


class LicenseKey(models.Model):
    """One row per issued key. Multiple keys may exist for the same
    tenant (key rotation), but only one should be ACTIVE at a time —
    enforced softly by the admin actions, not by a DB constraint."""

    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        REVOKED = 'REVOKED', 'Revoked'

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE, related_name='license_keys',
    )

    # sha256 of the cleartext key — the only persistent form. Indexed
    # because every heartbeat looks up by hash.
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)

    # First 8 chars of the cleartext. Support staff can ask the customer
    # for these to disambiguate which key they're using without having
    # the full key. Low information-leak (8 chars of base64url = 48 bits,
    # not enough to forge against the hash).
    key_prefix = models.CharField(max_length=8, db_index=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE,
        db_index=True,
    )

    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Subscription end date. NULL = no expiry (perpetual).',
    )

    # Banner text the POS will display next heartbeat. Use for short
    # advisories like "subscription expires in 3 days" or "scheduled
    # maintenance Friday". Kept short on purpose — anything long belongs
    # in email.
    message = models.CharField(max_length=500, blank=True, default='')

    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def issue(cls, tenant, *, expires_at=None, notes=''):
        """Create a fresh key for `tenant`. Returns (LicenseKey, cleartext).
        The cleartext is the only chance to expose it; persist nothing
        else."""
        cleartext = _new_key_cleartext()
        instance = cls.objects.create(
            tenant=tenant,
            key_hash=_hash_key(cleartext),
            key_prefix=cleartext[:8],
            expires_at=expires_at,
            notes=notes,
        )
        return instance, cleartext

    @classmethod
    def lookup_by_cleartext(cls, cleartext):
        """Lookup by SHA256 hash with a constant-time compare on the
        full hash to defend against partial-match timing leaks. SHA index
        narrows the scan to one row; the compare is just hygiene."""
        from django.utils.crypto import constant_time_compare
        if not cleartext or len(cleartext) < 8:
            return None
        target = _hash_key(cleartext)
        candidates = cls.objects.filter(key_prefix=cleartext[:8])
        for candidate in candidates:
            if constant_time_compare(candidate.key_hash, target):
                return candidate
        return None

    def __str__(self):
        return f'LicenseKey<{self.key_prefix}… {self.tenant.org_name} {self.status}>'
