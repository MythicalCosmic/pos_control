"""Tenants: one row per restaurant / chain that's been issued an
invite, regardless of whether they've redeemed it.

InviteCode is the gating mechanism — registration is not self-serve;
the vendor (you) issues a one-time code, the customer enters it in the
alpha_pos setup wizard along with their email + org name, and the
control center exchanges the code for a license key.
"""
import secrets

from django.db import models


class Tenant(models.Model):
    """A restaurant (or a chain, eventually) the vendor has signed up.

    Email is the natural key but display uses org_name. `notes` is a
    free-text scratchpad for the support staff — kept off the heartbeat
    response so customers never see it.
    """
    org_name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    notes = models.TextField(blank=True, default='')

    # Prepaid wallet. The subscription charges its price from here once per
    # period (see billing.services). When it can't cover the next period the
    # heartbeat reports EXPIRED and the POS kill switch fires. Topped up via
    # the admin "Add credit" action or a payment provider (Click / Payme).
    # Never write this directly outside billing.services — go through
    # credit_balance()/settle() so the Payment ledger stays consistent.
    balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text='Prepaid balance. Charged per subscription period.',
    )

    # External payment-provider customer reference (Click / Payme / Stripe).
    billing_external_id = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Payment provider customer ID (or similar). Optional.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['org_name']

    def __str__(self):
        return f'{self.org_name} ({self.email})'


def _new_invite_code():
    """16-char URL-safe code. Length chosen to balance copy-pasteability
    (short enough to type from an email) with brute-force resistance
    (16 chars of base64url ≈ 96 bits)."""
    return secrets.token_urlsafe(12)[:16]


class InviteCode(models.Model):
    """One-shot ticket the customer redeems via /api/v1/register.

    Issued by staff in the Django admin, sent to the customer over
    whatever channel (email, Telegram, paper). After redemption it's
    bound to the resulting Tenant and the consumed_at timestamp is set;
    second redemption attempts return 409.
    """
    code = models.CharField(
        max_length=32, unique=True, default=_new_invite_code, db_index=True,
    )

    # The tenant the code created when redeemed. NULL until the customer
    # actually hits /register.
    tenant = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invite_codes',
    )

    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='If set, registrations with this code after this time are refused.',
    )
    consumed_at = models.DateTimeField(null=True, blank=True)

    # Optional pre-fill: lets staff sell to a specific customer ahead of
    # time. The wizard's `email` / `org_name` payload must match these
    # (case-insensitive) if set — protects against an invite being
    # intercepted and redeemed by someone else.
    intended_email = models.EmailField(blank=True, default='')
    intended_org_name = models.CharField(max_length=200, blank=True, default='')

    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def is_expired(self, *, now=None) -> bool:
        if self.expires_at is None:
            return False
        from django.utils import timezone
        return (now or timezone.now()) > self.expires_at

    def __str__(self):
        state = 'consumed' if self.is_consumed() else 'unused'
        return f'InviteCode<{self.code} {state}>'
