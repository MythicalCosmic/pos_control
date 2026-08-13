from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from billing.services.billing import resolve
from licenses.models import LicenseKey


def iso(value):
    if value is None:
        return None
    if timezone.is_aware(value):
        value = value.astimezone(datetime_timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def money(value):
    return f"{Decimal(value):.2f}"


def health_thresholds():
    return {
        "online_minutes": settings.HEARTBEAT_ONLINE_MINUTES,
        "delayed_minutes": settings.HEARTBEAT_DELAYED_MINUTES,
    }


def heartbeat_health(received_at, *, now=None):
    if received_at is None:
        return "QUIET"
    age = (now or timezone.now()) - received_at
    if age <= timedelta(minutes=settings.HEARTBEAT_ONLINE_MINUTES):
        return "ONLINE"
    if age <= timedelta(minutes=settings.HEARTBEAT_DELAYED_MINUTES):
        return "DELAYED"
    return "QUIET"


def plan_data(plan, *, subscriber_count=None):
    if plan is None:
        return None
    data = {
        "id": plan.pk,
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "price": money(plan.price),
        "period_days": plan.period_days,
        "warn_days": plan.warn_days,
        "grace_days": plan.grace_days,
        "is_active": plan.is_active,
        "sort_order": plan.sort_order,
        "created_at": iso(plan.created_at),
        "updated_at": iso(plan.updated_at),
    }
    if subscriber_count is not None:
        data["subscriber_count"] = subscriber_count
    return data


def subscription_data(subscription, *, now=None):
    if subscription is None:
        return None
    now = now or timezone.now()
    result = resolve(subscription.tenant, now=now, charge=False)
    if result.status == "EXPIRED":
        billing_state = "EXPIRED"
    elif result.in_grace:
        billing_state = "GRACE"
    elif result.warn:
        billing_state = "WARNING"
    else:
        billing_state = "PAID"
    return {
        "id": subscription.pk,
        "tenant": tenant_ref(subscription.tenant),
        "status": subscription.status,
        "plan": plan_data(subscription.plan) if subscription.plan_id else None,
        "price": money(subscription.price),
        "period_days": subscription.period_days,
        "paid_through": iso(subscription.paid_through),
        "warn_days": subscription.warn_days,
        "grace_days": subscription.grace_days,
        "billing_state": billing_state,
        "days_remaining": result.days_remaining,
        "last_charged_at": iso(subscription.last_charged_at),
        "created_at": iso(subscription.created_at),
        "updated_at": iso(subscription.updated_at),
    }


def last_heartbeat(license_key):
    cached = getattr(license_key, "_latest_heartbeat", None)
    if cached is not None:
        return cached
    return license_key.heartbeat_events.order_by("-received_at").first()


def tenant_ref(tenant):
    return {"id": tenant.pk, "org_name": tenant.org_name, "email": tenant.email}


def license_data(license_key, *, now=None, include_notes=True):
    hb = last_heartbeat(license_key)
    computed = license_key.status
    if license_key.status == LicenseKey.Status.ACTIVE:
        result = resolve(license_key.tenant, now=now, charge=False)
        computed = result.status
    data = {
        "id": license_key.pk,
        "tenant": tenant_ref(license_key.tenant),
        "key_prefix": license_key.key_prefix,
        "status": license_key.status,
        "computed_status": computed,
        "message": license_key.message,
        "created_at": iso(license_key.created_at),
        "revoked_at": iso(license_key.revoked_at),
        "last_heartbeat": None,
    }
    if include_notes:
        data["notes"] = license_key.notes
    if hb:
        data["last_heartbeat"] = {
            "received_at": iso(hb.received_at),
            "health": heartbeat_health(hb.received_at, now=now),
            "client_version": hb.client_version,
            "branch_id": hb.branch_id,
            "fingerprint": hb.fingerprint,
            "ip": hb.ip,
        }
    return data


def tenant_data(tenant, *, now=None):
    now = now or timezone.now()
    licenses = list(tenant.license_keys.all())
    latest = None
    counts = {"total": len(licenses), "online": 0, "suspended": 0, "revoked": 0}
    for key in licenses:
        hb = last_heartbeat(key)
        if hb and (latest is None or hb.received_at > latest):
            latest = hb.received_at
        if hb and heartbeat_health(hb.received_at, now=now) == "ONLINE":
            counts["online"] += 1
        if key.status == LicenseKey.Status.SUSPENDED:
            counts["suspended"] += 1
        elif key.status == LicenseKey.Status.REVOKED:
            counts["revoked"] += 1
    sub = getattr(tenant, "subscription", None)
    sub_data = subscription_data(sub, now=now)
    non_revoked = counts["total"] - counts["revoked"]
    if counts["total"] and counts["revoked"] == counts["total"]:
        computed = "REVOKED"
    elif non_revoked and counts["suspended"] == non_revoked:
        computed = "SUSPENDED"
    elif sub_data and sub_data["billing_state"] == "EXPIRED":
        computed = "EXPIRED"
    else:
        computed = "ACTIVE"
    return {
        "id": tenant.pk,
        "org_name": tenant.org_name,
        "email": tenant.email,
        "notes": tenant.notes,
        "balance": money(tenant.balance),
        "created_at": iso(tenant.created_at),
        "updated_at": iso(tenant.updated_at),
        "computed_status": computed,
        "install_counts": counts,
        "last_heartbeat_at": iso(latest),
        "subscription": sub_data,
    }


def invite_state(invite, *, now=None):
    if invite.revoked_at:
        return "REVOKED"
    if invite.consumed_at:
        return "CONSUMED"
    if invite.is_expired(now=now):
        return "EXPIRED"
    return "UNUSED"


def invite_data(invite, *, include_code=False, now=None):
    data = {
        "id": invite.pk,
        "state": invite_state(invite, now=now),
        "intended_email": invite.intended_email,
        "intended_org_name": invite.intended_org_name,
        "expires_at": iso(invite.expires_at),
        "consumed_at": iso(invite.consumed_at),
        "revoked_at": iso(invite.revoked_at),
        "notes": invite.notes,
        "tenant": tenant_ref(invite.tenant) if invite.tenant_id else None,
        "created_at": iso(invite.created_at),
    }
    if include_code:
        data["code"] = invite.code
    return data


def actor_data(actor):
    if actor is None:
        return None
    return {
        "id": actor.pk,
        "name": actor.get_full_name().strip() or actor.username,
        "username": actor.username,
    }


def control_event_data(event):
    return {
        "id": event.pk,
        "action": event.action,
        "actor": actor_data(event.actor),
        "tenant": tenant_ref(event.tenant) if event.tenant_id else None,
        "license": (
            {"id": event.license_key_id, "key_prefix": event.license_key.key_prefix}
            if event.license_key_id
            else None
        ),
        "metadata": event.metadata,
        "created_at": iso(event.created_at),
    }


def heartbeat_event_data(event, *, include_payload=False, now=None):
    data = {
        "id": event.pk,
        "ack_id": str(event.ack_id),
        "tenant": tenant_ref(event.license_key.tenant),
        "license": {
            "id": event.license_key_id,
            "key_prefix": event.license_key.key_prefix,
        },
        "received_at": iso(event.received_at),
        "health": heartbeat_health(event.received_at, now=now),
        "ip": event.ip,
        "client_version": event.client_version,
        "branch_id": event.branch_id,
        "fingerprint": event.fingerprint,
    }
    if include_payload:
        data["payload"] = event.payload
    return data


def payment_data(payment):
    external = payment.external_id
    if external and payment.source in ("CLICK", "PAYME"):
        external = f"••••{external[-4:]}" if len(external) > 4 else "••••"
    return {
        "id": payment.pk,
        "tenant": tenant_ref(payment.tenant),
        "amount": money(payment.amount),
        "kind": payment.kind,
        "source": payment.source,
        "external_id": external,
        "balance_after": money(payment.balance_after),
        "actor": actor_data(payment.actor),
        "note": payment.note,
        "created_at": iso(payment.created_at),
    }


def plan_change_data(change):
    return {
        "id": change.pk,
        "tenant": tenant_ref(change.tenant),
        "current_plan": plan_data(change.current_plan)
        if change.current_plan_id
        else None,
        "requested_plan": plan_data(change.requested_plan),
        "status": change.status,
        "note": change.note,
        "requested_at": iso(change.requested_at),
        "decided_at": iso(change.decided_at),
        "decided_by": actor_data(change.decided_by),
        "decision_note": change.decision_note,
    }
