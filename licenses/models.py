"""License keys + the events that affect them.

Storage rule: never the cleartext key. The customer receives the key
exactly once (in the /register response); the control center keeps only
its sha256 hash plus the first 8 chars of the cleartext for support
lookup. If the customer loses their key, they get a new one — there is
no recovery of the old.
"""

import hashlib
import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


KEY_LENGTH_BYTES = 48  # → 64 chars urlsafe base64


def _new_key_cleartext() -> str:
    """Return a fresh license key string. Caller stores the sha256 and
    returns the cleartext to the customer exactly once."""
    return secrets.token_urlsafe(KEY_LENGTH_BYTES)


def _hash_key(cleartext: str) -> str:
    return hashlib.sha256(cleartext.encode("utf-8")).hexdigest()


class LicenseKey(models.Model):
    """One row per issued key. Multiple keys may exist for the same
    tenant (key rotation), but only one should be ACTIVE at a time —
    enforced softly by the admin actions, not by a DB constraint."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        REVOKED = "REVOKED", "Revoked"

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="license_keys",
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
        max_length=12,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Subscription end date. NULL = no expiry (perpetual).",
    )

    # Banner text the POS will display next heartbeat. Use for short
    # advisories like "subscription expires in 3 days" or "scheduled
    # maintenance Friday". Kept short on purpose — anything long belongs
    # in email.
    message = models.CharField(max_length=500, blank=True, default="")

    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, tenant, *, expires_at=None, notes=""):
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

    def clean(self):
        super().clean()
        if self.pk and self.status != self.Status.REVOKED:
            was_revoked = (
                type(self)
                .objects.filter(
                    pk=self.pk,
                    status=self.Status.REVOKED,
                )
                .exists()
            )
            if was_revoked:
                raise ValidationError(
                    {
                        "status": "A revoked license cannot be reactivated.",
                    }
                )

    def save(self, *args, **kwargs):
        if self.pk and self.status != self.Status.REVOKED:
            was_revoked = (
                type(self)
                .objects.filter(
                    pk=self.pk,
                    status=self.Status.REVOKED,
                )
                .exists()
            )
            if was_revoked:
                raise ValidationError("A revoked license cannot be reactivated.")
        if self.status == self.Status.REVOKED and self.revoked_at is None:
            self.revoked_at = timezone.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"revoked_at"}
        return super().save(*args, **kwargs)

    def computed_status(self, *, now=None):
        """Resolve what the next heartbeat should report.

        Manual REVOKED / SUSPENDED on the key always win. Otherwise the
        verdict is money-driven: the tenant's subscription is settled (a
        period is charged from the prepaid balance if one is due and
        affordable) and the result is ACTIVE if the subscription is paid
        through, else EXPIRED. The legacy `expires_at` column is no longer
        consulted — `billing.Subscription.paid_through` is the source of
        truth.
        """
        if self.status == self.Status.REVOKED:
            return self.Status.REVOKED
        if self.status == self.Status.SUSPENDED:
            return self.Status.SUSPENDED
        from billing.services.billing import resolve

        return resolve(self.tenant, now=now).status

    def __str__(self):
        return f"LicenseKey<{self.key_prefix}… {self.tenant.org_name} {self.status}>"


class HeartbeatEvent(models.Model):
    """One row per /api/v1/heartbeat call. Kept verbose for now;
    aggressive retention (30 days raw, then daily rollups) is on the
    roadmap once the table starts growing.

    `ack_id` is echoed back in the response so the client knows which
    server replied — useful when debugging multi-instance / CDN setups."""

    import uuid as _uuid

    license_key = models.ForeignKey(
        LicenseKey,
        on_delete=models.CASCADE,
        related_name="heartbeat_events",
    )
    ack_id = models.UUIDField(default=_uuid.uuid4, editable=False, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    ip = models.GenericIPAddressField(null=True, blank=True)
    client_version = models.CharField(max_length=120, blank=True, default="")
    branch_id = models.CharField(max_length=120, blank=True, default="")
    fingerprint = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )

    # Original request payload kept verbatim for support diagnostics.
    # Reasonable size — heartbeat bodies are tiny.
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["license_key", "-received_at"]),
        ]

    def __str__(self):
        return f"HeartbeatEvent<{self.ack_id} {self.license_key.key_prefix}…>"


class ControlEvent(models.Model):
    """Audit trail for admin actions (suspend, resume, extend expiry,
    set message, generate unlock, etc.). Append-only — the admin must
    never edit or delete these. Filled out by the admin action commit;
    this model is here now so its migration goes out with the heartbeat
    table rather than as a churn-y add later."""

    class Action(models.TextChoices):
        SUSPEND = "SUSPEND", "Suspend"
        RESUME = "RESUME", "Resume"
        REVOKE = "REVOKE", "Revoke"
        EXTEND_EXPIRY = "EXTEND_EXPIRY", "Extend expiry"
        SET_MESSAGE = "SET_MESSAGE", "Set banner message"
        INVITE_CREATE = "INVITE_CREATE", "Invite code created"
        INVITE_REVOKE = "INVITE_REVOKE", "Invite code revoked"
        PLAN_CHANGE_APPROVE = "PLAN_CHANGE_APPROVE", "Plan change approved"
        PLAN_CHANGE_REJECT = "PLAN_CHANGE_REJECT", "Plan change rejected"
        UNLOCK_GENERATED = "UNLOCK_GENERATED", "Perpetual unlock generated"

    actor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="control_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    license_key = models.ForeignKey(
        LicenseKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="control_events",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="control_events",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ControlEvent<{self.action} @ {self.created_at:%Y-%m-%d %H:%M}>"
