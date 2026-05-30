"""Email nudges so a tenant never gets locked out by surprise.

Three states the daily ``bill_subscriptions`` cron emails on:

- **warn** — paid_through is still in the future but within ``warn_days``.
  ("Top up soon — service ends in N days.")
- **grace** — paid_through has passed; the tenant is in the ``grace_days``
  soft-cushion window before the kill switch fires.
  ("Payment is overdue — service will stop in N days.")
- **lockout** — grace exhausted; the next heartbeat will report EXPIRED
  and the POS kill switch will fire (or already has).
  ("Service has been paused — top up to restore.")

Each state has its own ``last_*_sent_at`` timestamp on the Subscription;
nothing fires more than once per ``THROTTLE_HOURS`` so a misconfigured cron
that runs hourly can't email the tenant twelve times before lunch."""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from billing.models import Subscription


logger = logging.getLogger(__name__)


# 20 h, not 24, so the daily cron is allowed to drift a couple of hours
# without skipping an entire day.
THROTTLE_HOURS = 20


def _from_address() -> str:
    return getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@control.local')


def _send(to: str, subject: str, body: str) -> bool:
    """Wrap ``send_mail`` so a bad SMTP config logs and moves on instead of
    crashing the cron. Returns True iff the backend accepted the message."""
    if not to:
        return False
    try:
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=_from_address(),
            recipient_list=[to],
            fail_silently=False,
        )
        return bool(sent)
    except Exception:
        logger.exception('billing.notifications: send_mail failed to %s', to)
        return False


def _throttled(last_sent, now) -> bool:
    if last_sent is None:
        return False
    return (now - last_sent) < timedelta(hours=THROTTLE_HOURS)


def _mark(sub: Subscription, field: str, now) -> None:
    setattr(sub, field, now)
    Subscription.objects.filter(pk=sub.pk).update(**{field: now})


def send_warn_email(sub: Subscription, *, days_remaining: int, now=None) -> bool:
    """'Top up soon' — sent while still ACTIVE but inside the warn window."""
    now = now or timezone.now()
    if _throttled(sub.last_warn_sent_at, now):
        return False
    tenant = sub.tenant
    subject = f'Your POS subscription renews in {days_remaining} days'
    body = (
        f'Hi {tenant.org_name},\n\n'
        f'Your POS subscription is paid through {sub.paid_through:%Y-%m-%d %H:%M UTC}.\n'
        f'That is in {days_remaining} day(s). Current balance: {tenant.balance}.\n'
        f'The next charge is {sub.price} so\'m for {sub.period_days} days.\n\n'
        f'Top up via Click.uz or Payme.uz to avoid an interruption.\n'
    )
    if _send(tenant.email, subject, body):
        _mark(sub, 'last_warn_sent_at', now)
        return True
    return False


def send_grace_email(sub: Subscription, *, days_left_in_grace: int, now=None) -> bool:
    """'Payment overdue, service stops in N days' — sent during grace."""
    now = now or timezone.now()
    if _throttled(sub.last_grace_sent_at, now):
        return False
    tenant = sub.tenant
    subject = f'POS payment overdue — service stops in {days_left_in_grace} days'
    body = (
        f'Hi {tenant.org_name},\n\n'
        f'Your POS subscription renewal failed — the prepaid balance ({tenant.balance})\n'
        f'cannot cover the {sub.price} so\'m charge.\n\n'
        f'You have {days_left_in_grace} day(s) of grace remaining. After that,\n'
        f'the POS will stop accepting orders until you top up via Click.uz or Payme.uz.\n'
    )
    if _send(tenant.email, subject, body):
        _mark(sub, 'last_grace_sent_at', now)
        return True
    return False


def send_lockout_email(sub: Subscription, *, now=None) -> bool:
    """'Service paused' — sent once after the grace window has fully elapsed."""
    now = now or timezone.now()
    if _throttled(sub.last_lockout_sent_at, now):
        return False
    tenant = sub.tenant
    subject = 'POS service has been paused — top up to restore'
    body = (
        f'Hi {tenant.org_name},\n\n'
        f'Your POS service is paused: the prepaid balance ({tenant.balance})\n'
        f'cannot cover the {sub.price} so\'m subscription charge and the grace\n'
        f'window has expired.\n\n'
        f'Top up via Click.uz or Payme.uz. The POS will come back online on the\n'
        f'next heartbeat (within ~5 minutes of the payment landing).\n'
    )
    if _send(tenant.email, subject, body):
        _mark(sub, 'last_lockout_sent_at', now)
        return True
    return False
