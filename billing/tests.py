"""Tests for the prepaid-balance + subscription billing service, the grace
period cushion, and the warning-email pipeline driven by
``bill_subscriptions``."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from billing.models import Payment, Subscription
from billing.services.billing import credit_balance, resolve, settle
from billing.services import notifications
from tenants.models import Tenant


pytestmark = pytest.mark.django_db


def _tenant(balance=0, **kwargs):
    return Tenant.objects.create(
        org_name=kwargs.pop('org_name', 'Café'),
        email=kwargs.pop('email', 'c@x.local'),
        balance=balance,
    )


class TestSettle:
    def test_charges_one_period_and_advances_paid_through(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(tenant=t, price=Decimal('30'), period_days=30)

        settle(sub)

        sub.refresh_from_db()
        t.refresh_from_db()
        assert t.balance == Decimal('70.00')
        assert sub.paid_through is not None
        assert sub.last_charged_at is not None
        charge = Payment.objects.get(tenant=t, kind=Payment.Kind.CHARGE)
        assert charge.amount == Decimal('30')
        assert charge.balance_after == Decimal('70.00')

    def test_not_due_is_noop(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(tenant=t, price=Decimal('30'), period_days=30)
        settle(sub)
        t.refresh_from_db()
        assert t.balance == Decimal('70.00')

        settle(sub)
        t.refresh_from_db()
        assert t.balance == Decimal('70.00')
        assert Payment.objects.filter(kind=Payment.Kind.CHARGE).count() == 1

    def test_insufficient_balance_leaves_unpaid(self):
        t = _tenant(balance=Decimal('5'))
        sub = Subscription.objects.create(tenant=t, price=Decimal('30'), period_days=30)
        settle(sub)
        sub.refresh_from_db()
        t.refresh_from_db()
        assert sub.paid_through is None
        assert t.balance == Decimal('5.00')
        assert not Payment.objects.filter(kind=Payment.Kind.CHARGE).exists()

    def test_due_again_after_period_charges_again(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(tenant=t, price=Decimal('30'), period_days=30)
        settle(sub)
        Subscription.objects.filter(pk=sub.pk).update(
            paid_through=timezone.now() - timedelta(seconds=1),
        )
        sub.refresh_from_db()
        settle(sub)
        t.refresh_from_db()
        assert t.balance == Decimal('40.00')
        assert Payment.objects.filter(kind=Payment.Kind.CHARGE).count() == 2


class TestResolve:
    def test_active_when_paid_through_future(self):
        t = _tenant(balance=Decimal('100'))
        Subscription.objects.create(tenant=t, price=Decimal('30'), period_days=30)
        result = resolve(t)
        assert result.status == 'ACTIVE'
        assert result.balance == Decimal('70.00')
        assert result.days_remaining is not None
        assert result.warn is False
        assert result.in_grace is False

    def test_expired_when_unpaid_and_no_grace_left(self):
        t = _tenant(balance=Decimal('0'))
        # grace_days=0 → instant lockout the moment paid_through lapses.
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        result = resolve(t)
        assert result.status == 'EXPIRED'
        assert result.in_grace is False

    def test_free_plan_is_active(self):
        t = _tenant()
        Subscription.objects.create(tenant=t, price=Decimal('0'), period_days=30)
        result = resolve(t)
        assert result.status == 'ACTIVE'
        assert result.days_remaining is None

    def test_no_subscription_is_active(self):
        t = _tenant()
        result = resolve(t)
        assert result.status == 'ACTIVE'
        assert result.in_grace is False

    def test_charge_false_does_not_settle(self):
        t = _tenant(balance=Decimal('100'))
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        result = resolve(t, charge=False)
        # No charge attempted → wallet untouched, and (never paid) → EXPIRED.
        assert result.status == 'EXPIRED'
        t.refresh_from_db()
        assert t.balance == Decimal('100.00')

    def test_warn_flips_within_window(self):
        t = _tenant(balance=Decimal('100'))
        Subscription.objects.create(
            tenant=t, price=Decimal('10'), period_days=2, warn_days=5,
        )
        result = resolve(t)
        assert result.status == 'ACTIVE'
        assert result.warn is True
        assert result.days_remaining <= 5
        assert result.in_grace is False


class TestGracePeriod:
    """A missed renewal stays ACTIVE with in_grace=True/warn=True for up to
    grace_days, so a busy restaurant isn't bricked the instant a payment
    misses. After that, status flips to EXPIRED and the kill switch fires."""

    def test_in_grace_keeps_active_and_warns(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=3,
        )
        settle(sub)
        # Force paid_through 1 hour into the past — still well inside the
        # 3-day grace cushion.
        Subscription.objects.filter(pk=sub.pk).update(
            paid_through=timezone.now() - timedelta(hours=1),
        )

        # Block the auto-resettle by emptying the wallet — only grace logic
        # can keep this ACTIVE now.
        Tenant.objects.filter(pk=t.pk).update(balance=Decimal('0'))

        result = resolve(t)
        assert result.status == 'ACTIVE'
        assert result.in_grace is True
        assert result.warn is True

    def test_grace_exhausted_flips_expired(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=3,
        )
        settle(sub)
        # 4 days past paid_through → grace exhausted.
        Subscription.objects.filter(pk=sub.pk).update(
            paid_through=timezone.now() - timedelta(days=4),
        )
        Tenant.objects.filter(pk=t.pk).update(balance=Decimal('0'))

        result = resolve(t)
        assert result.status == 'EXPIRED'
        assert result.in_grace is False

    def test_zero_grace_means_instant_lockout(self):
        t = _tenant(balance=Decimal('0'))
        sub = Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        # No paid_through ever → straight to EXPIRED, no grace.
        result = resolve(t)
        assert result.status == 'EXPIRED'
        assert result.in_grace is False


class TestCreditBalance:
    def test_topup_adds_and_records_ledger(self):
        t = _tenant()
        payment = credit_balance(t, Decimal('50'), source=Payment.Source.CLICK,
                                 external_id='tx-credit-1')
        t.refresh_from_db()
        assert t.balance == Decimal('50.00')
        assert payment.kind == Payment.Kind.TOPUP
        assert payment.balance_after == Decimal('50.00')

    def test_topup_revives_expired_tenant(self):
        t = _tenant(balance=Decimal('0'))
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        assert resolve(t).status == 'EXPIRED'

        credit_balance(t, Decimal('50'), source=Payment.Source.CLICK,
                       external_id='tx-revive-1')
        result = resolve(t)
        assert result.status == 'ACTIVE'
        t.refresh_from_db()
        assert t.balance == Decimal('20.00')

    def test_idempotent_on_source_external_id(self):
        t = _tenant()
        p1 = credit_balance(t, Decimal('50'), source=Payment.Source.CLICK, external_id='tx-1')
        p2 = credit_balance(t, Decimal('50'), source=Payment.Source.CLICK, external_id='tx-1')
        assert p1.pk == p2.pk
        t.refresh_from_db()
        assert t.balance == Decimal('50.00')
        assert Payment.objects.filter(
            source=Payment.Source.CLICK, external_id='tx-1',
        ).count() == 1

    def test_rejects_non_positive(self):
        t = _tenant()
        with pytest.raises(ValueError):
            credit_balance(t, Decimal('0'), source=Payment.Source.CLICK,
                           external_id='tx-zero')

    def test_rejects_amount_over_safety_cap(self):
        t = _tenant()
        with pytest.raises(ValueError):
            credit_balance(
                t, Decimal('999999999999'),  # well above MAX_CREDIT_AMOUNT
                source=Payment.Source.CLICK, external_id='tx-huge',
            )


class TestWarningEmails:
    """``send_warn_email`` / ``send_grace_email`` / ``send_lockout_email`` each
    fire at most once per ~20 hours per tenant (THROTTLE_HOURS) so a cron that
    runs hourly can't spam the operator with duplicate nudges."""

    def _seed(self, *, balance=Decimal('100'), grace_days=3,
              warn_days=3, period_days=30, price=Decimal('30')):
        t = _tenant(balance=balance)
        sub = Subscription.objects.create(
            tenant=t, price=price, period_days=period_days,
            warn_days=warn_days, grace_days=grace_days,
        )
        return t, sub

    def test_warn_email_sent_once_then_throttled(self):
        t, sub = self._seed(period_days=2, warn_days=5)  # always inside warn window
        settle(sub)

        sent = notifications.send_warn_email(sub, days_remaining=1)
        assert sent is True
        assert len(mail.outbox) == 1
        assert t.email in mail.outbox[0].to
        assert 'renews in' in mail.outbox[0].subject

        # Second call same day → throttled, no second email.
        again = notifications.send_warn_email(sub, days_remaining=1)
        assert again is False
        assert len(mail.outbox) == 1

    def test_grace_email_fires_during_grace_cushion(self):
        t, sub = self._seed(balance=Decimal('0'))
        Subscription.objects.filter(pk=sub.pk).update(
            paid_through=timezone.now() - timedelta(hours=2),
        )
        sub.refresh_from_db()

        sent = notifications.send_grace_email(sub, days_left_in_grace=2)
        assert sent is True
        assert any('overdue' in m.subject.lower() for m in mail.outbox)

    def test_lockout_email_fires_once(self):
        t, sub = self._seed(balance=Decimal('0'), grace_days=0)
        sent = notifications.send_lockout_email(sub)
        assert sent is True
        assert any('paused' in m.subject.lower() for m in mail.outbox)

        # Throttled on repeat.
        sent_again = notifications.send_lockout_email(sub)
        assert sent_again is False


class TestBillSubscriptionsCommand:
    """End-to-end through the cron entrypoint: settles every active
    subscription and emits at most one nudge per tenant per run."""

    def test_warn_path_sends_warn_email(self):
        t = _tenant(balance=Decimal('100'))
        sub = Subscription.objects.create(
            tenant=t, price=Decimal('10'), period_days=2, warn_days=5,
            grace_days=3,
        )
        settle(sub)

        out = StringIO()
        call_command('bill_subscriptions', stdout=out)
        assert 'warn=1' in out.getvalue()
        assert len(mail.outbox) == 1

    def test_grace_path_sends_grace_email(self):
        t = _tenant(balance=Decimal('0'))
        sub = Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=3,
        )
        # Anchor paid_through 1 day into the past — inside grace.
        Subscription.objects.filter(pk=sub.pk).update(
            paid_through=timezone.now() - timedelta(days=1),
        )

        out = StringIO()
        call_command('bill_subscriptions', stdout=out)
        assert 'grace=1' in out.getvalue()
        assert any('overdue' in m.subject.lower() for m in mail.outbox)

    def test_lockout_path_sends_lockout_email(self):
        t = _tenant(balance=Decimal('0'))
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        out = StringIO()
        call_command('bill_subscriptions', stdout=out)
        assert 'lockout=1' in out.getvalue()
        assert any('paused' in m.subject.lower() for m in mail.outbox)

    def test_no_email_flag_skips_sending(self):
        t = _tenant(balance=Decimal('0'))
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        out = StringIO()
        call_command('bill_subscriptions', '--no-email', stdout=out)
        assert mail.outbox == []
        # Status block still printed (with zeros).
        assert 'warn=0' in out.getvalue()

    def test_suspended_tenant_is_not_emailed(self):
        from licenses.models import LicenseKey
        t = _tenant(balance=Decimal('0'))
        Subscription.objects.create(
            tenant=t, price=Decimal('30'), period_days=30, grace_days=0,
        )
        row, _ = LicenseKey.issue(t)
        row.status = LicenseKey.Status.SUSPENDED
        row.save()

        out = StringIO()
        call_command('bill_subscriptions', stdout=out)
        assert mail.outbox == []
