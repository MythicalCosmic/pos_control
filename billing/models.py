"""Prepaid-balance billing: subscriptions draw a per-period price from a
tenant's wallet (``tenants.Tenant.balance``). When the wallet can't cover
the next period the heartbeat reports EXPIRED and the alpha_pos kill switch
fires.

Two models live here:

- ``Subscription`` — one per tenant. Holds the price, the period length, and
  ``paid_through`` (the moment service is good until). The vendor sets the
  price/period; ``paid_through`` is advanced by the billing service, never by
  hand.
- ``Payment`` — append-only ledger of every money movement (top-ups in,
  subscription charges out). Mirrors the ``licenses.ControlEvent`` audit
  pattern: read-only in the admin, never edited or deleted. Each row records
  the ``balance_after`` so the wallet's history is reconstructable.
"""
from django.db import models


class SubscriptionPlan(models.Model):
    """A vendor-published pricing tier. The setup wizard lists the active
    plans (``GET /api/v1/plans``); the customer picks one and the new
    Tenant's Subscription is bound to that plan with its fields copied in.
    Plans are the source of truth on the dashboard; per-tenant overrides
    are still possible by hand-editing the Subscription row but are the
    exception, not the rule.

    The fields mirror Subscription so plan→subscription is a straight copy
    and so a vendor can edit a Subscription independently without
    re-touching the Plan (e.g., a sweetheart deal for one customer)."""

    code = models.SlugField(
        max_length=32, unique=True,
        help_text='Short stable identifier (e.g. "basic", "pro").',
    )
    name = models.CharField(
        max_length=120,
        help_text='Display name shown to the customer in the setup wizard.',
    )
    description = models.TextField(
        blank=True, default='',
        help_text='One- or two-line pitch shown next to the price.',
    )
    price = models.DecimalField(
        max_digits=14, decimal_places=2,
        help_text='So\'m charged each period from the prepaid wallet.',
    )
    period_days = models.PositiveIntegerField(
        default=30, help_text='Length of one billing period, in days.',
    )
    warn_days = models.PositiveIntegerField(
        default=3, help_text='Days before cut-off to start warning the POS.',
    )
    grace_days = models.PositiveIntegerField(
        default=3,
        help_text='Days of soft cushion after paid_through before lockout.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Hide retired plans from the wizard without deleting them.',
    )
    sort_order = models.PositiveIntegerField(
        default=100,
        help_text='Display order — lower first.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'price']

    def __str__(self):
        return f'Plan<{self.code} {self.price}/{self.period_days}d>'


class Subscription(models.Model):
    """The recurring plan for one tenant. Charged ``price`` every
    ``period_days`` out of the tenant's balance."""

    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        CANCELED = 'CANCELED', 'Canceled'

    tenant = models.OneToOneField(
        'tenants.Tenant', on_delete=models.CASCADE, related_name='subscription',
    )

    # The plan the customer chose at setup (or via a vendor-approved
    # PlanChangeRequest). Nullable for two reasons: (a) the free
    # auto-created Subscription at registration when no plan was picked,
    # (b) legacy rows from before the plan catalog existed.
    plan = models.ForeignKey(
        'billing.SubscriptionPlan',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='subscriptions',
    )

    price = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text='Amount charged from the balance each period.',
    )
    period_days = models.PositiveIntegerField(
        default=30, help_text='Length of one billing period, in days.',
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE,
        db_index=True,
    )

    # Service is good until this moment. NULL = never charged yet (the first
    # settle() anchors the first period at "now"). Advanced one period each
    # time the balance covers a charge; the heartbeat reports EXPIRED once
    # `now` passes it and the balance can't fund the next period.
    paid_through = models.DateTimeField(null=True, blank=True)

    # How many days before `paid_through` the POS should start warning the
    # operator ("top up soon"). The "dynamic days" the vendor controls.
    warn_days = models.PositiveIntegerField(
        default=3, help_text='Days before cut-off to start warning the POS.',
    )

    # After `paid_through` passes, give the customer this many days of soft
    # cushion before the kill switch fires — the heartbeat keeps reporting
    # ACTIVE (with in_grace=True and warn=True) so a busy restaurant isn't
    # bricked the instant a renewal misses. Set to 0 for instant lockout.
    grace_days = models.PositiveIntegerField(
        default=3,
        help_text='Days of soft cushion after paid_through before lockout.',
    )

    last_charged_at = models.DateTimeField(null=True, blank=True)

    # Throttles for the warning emails sent by `bill_subscriptions` so a
    # tenant only gets one nudge per day per state. NULL = never sent.
    last_warn_sent_at = models.DateTimeField(null=True, blank=True)
    last_grace_sent_at = models.DateTimeField(null=True, blank=True)
    last_lockout_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Subscription<{self.tenant.org_name} {self.price}/{self.period_days}d {self.status}>'


class PlanChangeRequest(models.Model):
    """A customer asked to move to a different plan; the vendor must
    approve. Approval swaps the Tenant's Subscription.plan and copies the
    new plan's pricing in. Rejection is a no-op on billing. Either way
    leaves an audit row.

    One pending request per tenant at a time (DB-level partial unique
    constraint below) so the admin queue can't fill with duplicates if
    the customer hammers the button."""

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending vendor approval'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE,
        related_name='plan_change_requests',
    )
    current_plan = models.ForeignKey(
        'billing.SubscriptionPlan', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
        help_text='Snapshot of the plan the tenant was on when the request was filed.',
    )
    requested_plan = models.ForeignKey(
        'billing.SubscriptionPlan', on_delete=models.CASCADE,
        related_name='+',
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
        db_index=True,
    )
    note = models.CharField(max_length=255, blank=True, default='')

    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='plan_change_decisions',
    )
    decision_note = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-requested_at']
        constraints = [
            # Only one PENDING request per tenant at a time. APPROVED /
            # REJECTED rows accumulate freely for history.
            models.UniqueConstraint(
                fields=['tenant'],
                condition=models.Q(status='PENDING'),
                name='one_pending_plan_change_per_tenant',
            ),
        ]

    def __str__(self):
        return (f'PlanChangeRequest<{self.tenant.org_name} '
                f'{self.current_plan_id}→{self.requested_plan_id} {self.status}>')


class Payment(models.Model):
    """Append-only money ledger. One row per top-up or subscription charge.
    Never edited or deleted — the admin is read-only."""

    class Kind(models.TextChoices):
        TOPUP = 'TOPUP', 'Top-up (money in)'
        CHARGE = 'CHARGE', 'Subscription charge (money out)'
        ADJUST = 'ADJUST', 'Manual adjustment'

    class Source(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual (admin)'
        CLICK = 'CLICK', 'Click.uz'
        PAYME = 'PAYME', 'Payme.uz'
        SYSTEM = 'SYSTEM', 'System (subscription charge)'

    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.PROTECT, related_name='payments',
    )
    # Positive for money in (TOPUP), positive for money out too — the `kind`
    # disambiguates direction. balance_after is the source of truth for sign.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    kind = models.CharField(max_length=8, choices=Kind.choices, db_index=True)
    source = models.CharField(
        max_length=8, choices=Source.choices, default=Source.MANUAL,
        db_index=True,
    )

    # Provider transaction id (Click/Payme) — used for idempotency so a
    # retried webhook can't double-credit. Blank for manual/system rows.
    external_id = models.CharField(max_length=128, blank=True, default='')

    # Wallet balance immediately AFTER this row was applied.
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)

    actor = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments',
    )
    note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # A given provider transaction credits the wallet exactly once.
            # Partial (only when external_id is non-empty) so manual/system
            # rows with no external id aren't forced unique.
            models.UniqueConstraint(
                fields=['source', 'external_id'],
                condition=~models.Q(external_id=''),
                name='payment_unique_source_external_id',
            ),
        ]
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
        ]

    def __str__(self):
        return f'Payment<{self.kind} {self.amount} {self.tenant.org_name} → {self.balance_after}>'


class BillingRun(models.Model):
    """Successful completion record for the scheduled billing job."""
    completed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    charged_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    grace_count = models.PositiveIntegerField(default=0)
    lockout_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-completed_at']


class ClickTransaction(models.Model):
    """State for one Click.uz payment. Click's Prepare/Complete handshake
    expects the merchant to reserve the order on Prepare and finalize the
    *same* amount on Complete — so we persist the prepared amount keyed by
    ``click_trans_id`` and refuse a Complete whose amount doesn't match. The
    wallet credit on Complete is idempotent on this id."""

    click_trans_id = models.CharField(max_length=64, unique=True, db_index=True)
    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE, related_name='click_transactions',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # so'm
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'ClickTransaction<{self.click_trans_id} {self.tenant_id} done={self.completed}>'


class PaymeTransaction(models.Model):
    """State for one Payme (Paycom) transaction. Payme's Merchant API is
    stateful — CreateTransaction / PerformTransaction / CancelTransaction /
    CheckTransaction all key off the Payme-side ``payme_id`` and we must report
    the transaction's lifecycle back. The actual wallet credit happens on
    Perform via the shared ``credit_balance`` (idempotent on this id).

    Timestamps are Unix milliseconds, as Payme expects them on the wire."""

    class State(models.IntegerChoices):
        CREATED = 1, 'Created'
        COMPLETED = 2, 'Completed'
        CANCELLED = -1, 'Cancelled (before perform)'
        CANCELLED_AFTER_COMPLETE = -2, 'Cancelled (after perform)'

    payme_id = models.CharField(max_length=64, unique=True, db_index=True)
    tenant = models.ForeignKey(
        'tenants.Tenant', on_delete=models.CASCADE, related_name='payme_transactions',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # in so'm
    state = models.IntegerField(choices=State.choices, default=State.CREATED)

    create_time = models.BigIntegerField(default=0)   # ms
    perform_time = models.BigIntegerField(default=0)  # ms
    cancel_time = models.BigIntegerField(default=0)   # ms
    reason = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'PaymeTransaction<{self.payme_id} {self.tenant_id} state={self.state}>'
