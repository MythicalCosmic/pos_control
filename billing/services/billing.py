"""The billing brain. Everything that touches a tenant's wallet goes through
here so the ``Payment`` ledger and the wallet balance never drift apart.

Three entry points:

- ``settle(subscription, now)`` — charge one period from the wallet if a charge
  is due and affordable, advancing ``paid_through``. Idempotent per period:
  calling it repeatedly between periods is a no-op. Re-anchors the period at
  ``now`` on each charge, so a lapsed install that gets topped up gets a fresh
  full period rather than being billed for the downtime.
- ``credit_balance(tenant, amount, ...)`` — add money to the wallet, write a
  TOPUP ``Payment`` (idempotent on ``(source, external_id)``), then settle so a
  cut-off install revives on its very next heartbeat. Called only from the
  payment provider webhooks (Click.uz, Payme.uz); the control center has no
  manual top-up form — all credits land on one auditable rail.
- ``resolve(tenant, now)`` — settle, then report ``ACTIVE``/``EXPIRED`` plus the
  numbers the heartbeat sends to the POS (balance, days_remaining, warn,
  in_grace). Honors ``Subscription.grace_days``: a missed renewal stays ACTIVE
  with ``in_grace=True`` and ``warn=True`` for up to that many days before
  flipping to EXPIRED — so a busy restaurant isn't bricked the instant a
  monthly renewal slips by a few hours.

Concurrency: the money-mutating paths lock the tenant + subscription rows with
``select_for_update`` inside a transaction (same pattern as alpha_pos
``notifications/services/loyalty_service.redeem``), so two concurrent
heartbeats / webhooks can't double-charge or double-credit.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import Payment, Subscription
from tenants.models import Tenant


logger = logging.getLogger(__name__)


# Cap a single top-up so a misconfigured webhook can't
# credit an unrecoverably absurd amount in one shot. 1B so'm (~$80k) is two
# orders of magnitude above any realistic single restaurant top-up.
MAX_CREDIT_AMOUNT = Decimal('1000000000.00')


def bind_plan_to_subscription(subscription: Subscription, plan) -> None:
    """Copy catalog policy to a tenant subscription without billing it."""
    subscription.plan = plan
    subscription.price = plan.price
    subscription.period_days = plan.period_days
    subscription.warn_days = plan.warn_days
    subscription.grace_days = plan.grace_days
    subscription.status = Subscription.Status.ACTIVE


@dataclass
class BillingResult:
    status: str  # 'ACTIVE' or 'EXPIRED' (manual SUSPENDED/REVOKED handled by caller)
    paid_through: Optional[object]  # datetime or None
    balance: Decimal
    days_remaining: Optional[int]
    warn: bool
    # True while paid_through is in the past but we're still inside the
    # tenant's `grace_days` cushion. Status stays ACTIVE during grace; the
    # heartbeat sets `warn=True` so the POS banner can scream "pay now".
    in_grace: bool = False


def settle(subscription: Subscription, now=None) -> None:
    """Charge one period if due and affordable. No-op when not due, when the
    plan is free/canceled, or when the wallet can't cover the price (in which
    case ``paid_through`` is left in the past so ``resolve`` reports EXPIRED)."""
    now = now or timezone.now()

    # Cheap pre-checks without a lock — re-checked under the lock below.
    if subscription.status != Subscription.Status.ACTIVE:
        return
    if subscription.price <= 0:
        return  # free plan: never charged, always active (see resolve)

    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=subscription.pk)
        if sub.status != Subscription.Status.ACTIVE or sub.price <= 0:
            return
        if sub.paid_through is not None and sub.paid_through > now:
            return  # current period still has time left

        tenant = Tenant.objects.select_for_update().get(pk=sub.tenant_id)
        if tenant.balance < sub.price:
            return  # can't afford the next period → stays EXPIRED until top-up

        tenant.balance = tenant.balance - sub.price
        tenant.save(update_fields=['balance', 'updated_at'])

        sub.paid_through = now + timedelta(days=sub.period_days)
        sub.last_charged_at = now
        sub.save(update_fields=['paid_through', 'last_charged_at', 'updated_at'])

        Payment.objects.create(
            tenant=tenant,
            amount=sub.price,
            kind=Payment.Kind.CHARGE,
            source=Payment.Source.SYSTEM,
            balance_after=tenant.balance,
            note=f'subscription charge ({sub.period_days}d)',
        )

    # Keep the caller's in-memory objects fresh.
    subscription.paid_through = sub.paid_through
    subscription.last_charged_at = sub.last_charged_at


def credit_balance(tenant: Tenant, amount, *, source, external_id='',
                   actor=None, note='', kind=None) -> Payment:
    """Add money to ``tenant``'s wallet and record a ledger row. Idempotent on
    ``(source, external_id)`` so a retried provider webhook can't double-credit.
    Settles afterward so a cut-off install comes back to life immediately.

    Returns the Payment row (the pre-existing one if this was a duplicate)."""
    amount = Decimal(str(amount)).quantize(Decimal('0.01'))
    if amount <= 0:
        raise ValueError('credit amount must be positive')
    if amount > MAX_CREDIT_AMOUNT:
        # Stop a runaway gateway / misformed webhook before it lands in the
        # ledger. The provider should never legitimately send this much in one
        # call; let it 4xx and re-queue if it's real.
        raise ValueError('credit amount exceeds safety cap')
    kind = kind or Payment.Kind.TOPUP

    try:
        with transaction.atomic():
            if external_id:
                existing = Payment.objects.filter(
                    source=source, external_id=external_id,
                ).first()
                if existing is not None:
                    return existing  # already credited — idempotent replay

            locked = Tenant.objects.select_for_update().get(pk=tenant.pk)
            locked.balance = locked.balance + amount
            locked.save(update_fields=['balance', 'updated_at'])

            payment = Payment.objects.create(
                tenant=locked,
                amount=amount,
                kind=kind,
                source=source,
                external_id=external_id,
                balance_after=locked.balance,
                actor=actor,
                note=note,
            )
    except IntegrityError:
        # The (source, external_id) unique constraint fired — we lost the race
        # against a concurrent identical webhook. Only that partial constraint
        # (external_id non-empty) can raise here; return the winner's row.
        # An empty external_id can't trip it, so re-raise anything unexpected.
        if external_id:
            return Payment.objects.get(source=source, external_id=external_id)
        raise

    sub = Subscription.objects.filter(tenant_id=tenant.pk).first()
    if sub is not None:
        settle(sub)
    return payment


def resolve(tenant: Tenant, now=None, *, charge=True) -> BillingResult:
    """Optionally settle, then report the billing-derived status and the
    numbers the heartbeat forwards to the POS. Does NOT consider manual
    SUSPENDED/REVOKED on the LicenseKey — the heartbeat view layers those on
    top, and passes ``charge=False`` for a suspended tenant so a cut-off
    customer is never billed while paused.

    Grace window: once ``paid_through`` slips into the past, the tenant stays
    ACTIVE for up to ``sub.grace_days`` more days (``in_grace=True``,
    ``warn=True``). After that, status flips to EXPIRED and the alpha_pos kill
    switch fires.
    """
    now = now or timezone.now()

    sub = Subscription.objects.filter(tenant_id=tenant.pk).first()
    if sub is None:
        # No plan configured yet → ungated (don't brick a tenant the vendor
        # hasn't set up billing for). refresh balance for display.
        tenant.refresh_from_db(fields=['balance'])
        return BillingResult('ACTIVE', None, tenant.balance, None, False, False)

    if charge:
        settle(sub, now=now)
    tenant.refresh_from_db(fields=['balance'])
    sub.refresh_from_db()

    in_grace = False
    if sub.status == Subscription.Status.CANCELED:
        # Cancellation stops renewal but honors a period already paid for.
        # It must not leave a free/no-period subscription active forever.
        active = sub.paid_through is not None and sub.paid_through > now
    elif sub.price <= 0:
        active = True  # free plan
    else:
        if sub.paid_through is None:
            active = False
        elif sub.paid_through > now:
            active = True
        else:
            # Period lapsed. Stay ACTIVE while inside the grace cushion so a
            # missed renewal doesn't brick a restaurant mid-service.
            grace_until = sub.paid_through + timedelta(days=sub.grace_days)
            if grace_until > now:
                active = True
                in_grace = True
            else:
                active = False

    days_remaining = None
    warn = False
    if sub.paid_through is not None:
        seconds_left = (sub.paid_through - now).total_seconds()
        days_remaining = max(0, int(seconds_left // 86400))
        if active and sub.price > 0 and sub.status == Subscription.Status.ACTIVE:
            # Warn during the lead window OR during the grace cushion — both
            # are "pay now or get locked out" states for the operator.
            warn = in_grace or days_remaining <= sub.warn_days

    return BillingResult(
        status='ACTIVE' if active else 'EXPIRED',
        paid_through=sub.paid_through,
        balance=tenant.balance,
        days_remaining=days_remaining,
        warn=warn,
        in_grace=in_grace,
    )
