from django.conf import settings
from django.db import models


class VendorSession(models.Model):
    """Rotating opaque bearer-token pair; only token hashes are persisted."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vendor_sessions",
    )
    access_hash = models.CharField(max_length=64, unique=True, db_index=True)
    refresh_hash = models.CharField(max_length=64, unique=True, db_index=True)
    access_expires_at = models.DateTimeField(db_index=True)
    refresh_expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("tenants_write", "Can create and edit tenants"),
            ("licenses_control", "Can control licenses"),
            ("billing_approve", "Can approve billing plan changes"),
            ("invites_manage", "Can create and revoke invites"),
            ("plans_manage", "Can maintain subscription plans"),
            ("subscriptions_write", "Can edit subscription policy fields"),
            ("view_sensitive_heartbeat", "Can view raw heartbeat payloads"),
        ]


class LoginAttempt(models.Model):
    username = models.CharField(max_length=150, blank=True, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    success = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class IdempotencyRecord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=255)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=500)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "key"],
                name="vendor_idempotency_user_key",
            ),
        ]
        ordering = ["-created_at"]
