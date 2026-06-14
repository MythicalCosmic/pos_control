"""Seed the standard subscription plans shown in the setup wizard
(``GET /api/v1/plans``, which the desktop licensing/plans page reads).

Idempotent: only creates plans that don't already exist (matched by ``code``),
so re-running never clobbers a price you edited by hand. Tune prices/tiers
afterwards at ``/admin/billing/subscriptionplan/``.

    python manage.py seed_plans
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

# Prices are in so'm per 30-day period. Edit freely in the admin afterwards.
PLANS = [
    dict(code='basic', name='Basic', price=Decimal('150000'), sort_order=10,
         description='Single till. Core POS + cloud sync.'),
    dict(code='pro', name='Pro', price=Decimal('350000'), sort_order=20,
         description='Multi-till, analytics, and the Telegram delivery bot.'),
    dict(code='enterprise', name='Enterprise', price=Decimal('750000'), sort_order=30,
         description='Unlimited tills, priority support, custom integrations.'),
]


class Command(BaseCommand):
    help = 'Seed the standard subscription plans (idempotent — never clobbers edits).'

    def handle(self, *args, **opts):
        from billing.models import SubscriptionPlan
        created = 0
        for p in PLANS:
            obj, was_created = SubscriptionPlan.objects.get_or_create(
                code=p['code'],
                defaults=dict(
                    name=p['name'], description=p['description'], price=p['price'],
                    period_days=30, warn_days=3, grace_days=3, is_active=True,
                    sort_order=p['sort_order'],
                ),
            )
            created += int(was_created)
            tag = 'created' if was_created else 'exists '
            self.stdout.write(f'  {tag}  {obj.code:12} {obj.price}/{obj.period_days}d')
        self.stdout.write(self.style.SUCCESS(
            f'Plans ready: {created} created, {len(PLANS) - created} already existed.'))
