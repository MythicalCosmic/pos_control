"""Settle every active subscription and send warning emails. Run daily from cron.

Online installs settle lazily on each heartbeat, so this command mainly keeps
the dashboard honest for installs that have gone quiet: it charges any due
period from the wallet, flips a tenant to "unpaid" (paid_through in the past),
and emails the operator before the kill switch fires.

Three notification states (each throttled to one email per ~day per tenant):
- warn: ACTIVE, but within warn_days of cut-off → "top up soon".
- grace: paid_through past, inside grace_days cushion → "service stops in N days".
- lockout: grace exhausted → "service paused".

Idempotent — safe to run hourly if the operator wants tighter follow-up."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.models import BillingRun, Subscription
from billing.services.billing import resolve, settle
from billing.services.notifications import (
    send_grace_email, send_lockout_email, send_warn_email,
)
from licenses.models import LicenseKey


class Command(BaseCommand):
    help = (
        'Charge any due subscription period from each tenant\'s prepaid '
        'balance, and email warn/grace/lockout notices.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-email', action='store_true',
            help='Settle only; skip the warning emails (useful in tests).',
        )

    def handle(self, *args, **options):
        # Tenants whose newest license key is manually SUSPENDED/REVOKED are
        # paused — never bill or email a customer you've cut off. Mirrors the
        # heartbeat, which passes charge=False for a suspended tenant.
        paused = set()
        seen = set()
        for key in LicenseKey.objects.order_by('tenant_id', '-created_at'):
            if key.tenant_id in seen:
                continue
            seen.add(key.tenant_id)
            if key.status in (LicenseKey.Status.SUSPENDED, LicenseKey.Status.REVOKED):
                paused.add(key.tenant_id)

        charged = 0
        warn_sent = 0
        grace_sent = 0
        lockout_sent = 0

        now = timezone.now()
        send_emails = not options.get('no_email')

        for sub in Subscription.objects.filter(
            status=Subscription.Status.ACTIVE,
        ).exclude(tenant_id__in=paused).select_related('tenant'):
            before = sub.paid_through
            settle(sub)
            sub.refresh_from_db()
            if sub.paid_through != before:
                charged += 1

            if not send_emails or sub.price <= 0:
                continue

            result = resolve(sub.tenant, now=now, charge=False)

            # Sequence of states from healthy to dead. We pick at most one
            # nudge per run per tenant — never warn AND grace in the same
            # email blast.
            if result.status == 'EXPIRED':
                if send_lockout_email(sub, now=now):
                    lockout_sent += 1
            elif result.in_grace:
                # During grace `days_remaining` is 0 (paid_through is past);
                # surface the grace remainder instead.
                grace_left = max(0, sub.grace_days - max(
                    0, int((now - sub.paid_through).total_seconds() // 86400)
                ))
                if send_grace_email(sub, days_left_in_grace=grace_left, now=now):
                    grace_sent += 1
            elif result.warn:
                if send_warn_email(sub, days_remaining=result.days_remaining or 0, now=now):
                    warn_sent += 1

        BillingRun.objects.create(
            charged_count=charged, warning_count=warn_sent,
            grace_count=grace_sent, lockout_count=lockout_sent,
        )
        self.stdout.write(self.style.SUCCESS(
            f'Settled subscriptions; {charged} charged a new period. '
            f'Emails sent: warn={warn_sent} grace={grace_sent} lockout={lockout_sent}.'
        ))
