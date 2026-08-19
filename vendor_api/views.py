from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from billing.models import (
    BillingRun,
    Payment,
    PlanChangeRequest,
    Subscription,
    SubscriptionPlan,
)
from billing.services.billing import bind_plan_to_subscription
from licenses.models import ControlEvent, HeartbeatEvent, LicenseKey
from tenants.models import InviteCode, Tenant
from vendor_api.auth import data_response, error, idempotent, parse_json, vendor_auth
from vendor_api.pagination import paginate, paginate_list
from vendor_api.serializers import (
    control_event_data,
    health_thresholds,
    heartbeat_event_data,
    heartbeat_health,
    invite_data,
    license_data,
    money,
    payment_data,
    plan_change_data,
    plan_data,
    subscription_data,
    tenant_data,
)


MAX_MONEY = Decimal("999999999999.99")
MAX_POSITIVE_INTEGER = 2_147_483_647


def _allowed(request, permission):
    return request.user.is_superuser or request.user.has_perm(
        f"vendor_api.{permission}"
    )


def _permission(request, permission):
    if _allowed(request, permission):
        return None
    return error("permission_denied", "Insufficient staff permission.", 403)


def _field_error(field, message):
    return error("validation_error", "Validation failed.", 422, {field: message})


def _reject_unknown_fields(body, allowed):
    unknown = set(body) - set(allowed)
    if not unknown:
        return None
    return error(
        "validation_error",
        "Validation failed.",
        422,
        {field: "Unknown field." for field in sorted(unknown)},
    )


def _integer(value, field, *, minimum=1, maximum=MAX_POSITIVE_INTEGER):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ValueError
        return parsed, None
    except (TypeError, ValueError):
        return None, _field_error(
            field, f"Enter an integer from {minimum} to {maximum}."
        )


def _query_id(request, field):
    value = request.GET.get(field)
    if value in (None, ""):
        return None, None
    return _integer(value, field)


def _decimal_amount(value, field="price"):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
        if not amount.is_finite() or not Decimal("0") <= amount <= MAX_MONEY:
            raise InvalidOperation
        return amount, None
    except (InvalidOperation, TypeError, ValueError):
        return None, _field_error(
            field, f"Enter a decimal amount from 0.00 to {MAX_MONEY}."
        )


def _date(value, field):
    if value in (None, ""):
        return None, None
    if not isinstance(value, str):
        return None, _field_error(field, "Use an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, None
    except (TypeError, ValueError):
        return None, error(
            "validation_error",
            "Validation failed.",
            422,
            {field: "Use an ISO 8601 timestamp."},
        )


def _window(value):
    value = value or "30d"
    if not value.endswith("d"):
        return 30
    try:
        return max(1, min(int(value[:-1]), 366))
    except ValueError:
        return 30


def _valid_email(value):
    try:
        validate_email(value)
        return len(value) <= 254
    except ValidationError:
        return False


def _latest_heartbeats(licenses):
    for key in licenses:
        key._latest_heartbeat = key.heartbeat_events.order_by("-received_at").first()
    return licenses


@vendor_auth(methods={"GET"})
def overview(request):
    now = timezone.now()
    days = _window(request.GET.get("window"))
    since = now - timedelta(days=days)
    tenants = list(
        Tenant.objects.select_related("subscription__plan").prefetch_related(
            "license_keys",
        )
    )
    licenses = list(LicenseKey.objects.select_related("tenant").all())
    _latest_heartbeats(licenses)
    online = sum(
        heartbeat_health(
            getattr(k, "_latest_heartbeat", None).received_at
            if getattr(k, "_latest_heartbeat", None)
            else None,
            now=now,
        )
        == "ONLINE"
        for k in licenses
    )
    at_risk = 0
    mrr = Decimal("0")
    for tenant in tenants:
        sub = getattr(tenant, "subscription", None)
        if not sub or sub.status != Subscription.Status.ACTIVE:
            continue
        snap = subscription_data(sub, now=now)
        if snap["billing_state"] in ("WARNING", "GRACE", "EXPIRED"):
            at_risk += 1
        if sub.period_days:
            mrr += sub.price * Decimal(30) / Decimal(sub.period_days)
    daily = (
        Payment.objects.filter(kind=Payment.Kind.TOPUP, created_at__gte=since)
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(amount=Sum("amount"))
        .order_by("date")
    )
    quiet = [
        license_data(k, now=now)
        for k in licenses
        if heartbeat_health(
            getattr(k, "_latest_heartbeat", None).received_at
            if getattr(k, "_latest_heartbeat", None)
            else None,
            now=now,
        )
        == "QUIET"
    ][:10]
    recent = ControlEvent.objects.select_related(
        "actor", "tenant", "license_key"
    ).order_by("-created_at")[:10]
    metrics = {
        "tenants_total": len(tenants),
        "tenants_active": sum(
            tenant_data(t, now=now)["computed_status"] == "ACTIVE" for t in tenants
        ),
        "installs_total": len(licenses),
        "installs_online": online,
        "licenses_suspended": sum(
            k.status == LicenseKey.Status.SUSPENDED for k in licenses
        ),
        "subscriptions_at_risk": at_risk,
        "pending_plan_changes": PlanChangeRequest.objects.filter(
            status=PlanChangeRequest.Status.PENDING
        ).count(),
        "mrr": money(mrr),
        "wallet_balance_total": money(
            Tenant.objects.aggregate(total=Sum("balance"))["total"] or 0
        ),
    }
    return data_response(
        {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "metrics": metrics,
            "health_thresholds": health_thresholds(),
            "revenue_series": [
                {"date": str(row["date"]), "amount": money(row["amount"])}
                for row in daily
            ],
            "quiet_installs": quiet,
            "recent_events": [control_event_data(row) for row in recent],
        }
    )


@vendor_auth(methods={"GET", "POST"})
def tenants(request):
    if request.method == "POST":
        denied = _permission(request, "tenants_write")
        if denied:
            return denied
        return _create_tenant(request)
    qs = Tenant.objects.select_related("subscription__plan").prefetch_related(
        "license_keys"
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(org_name__icontains=q) | Q(email__icontains=q) | Q(notes__icontains=q)
        )
    plan = request.GET.get("plan", "").strip()
    if plan:
        if plan.isascii() and plan.isdigit():
            plan_id, err = _integer(plan, "plan")
            if err:
                return err
            qs = qs.filter(subscription__plan_id=plan_id)
        else:
            qs = qs.filter(subscription__plan__code=plan)
    status = request.GET.get("status", "").upper()
    qs = qs.distinct().order_by("org_name")
    if status in ("ACTIVE", "SUSPENDED", "REVOKED", "EXPIRED"):
        rows = [tenant_data(row) for row in qs]
        return paginate_list(
            request, [row for row in rows if row["computed_status"] == status]
        )
    return paginate(request, qs, tenant_data)


@idempotent
def _create_tenant(request):
    body, err = parse_json(request)
    if err:
        return err
    err = _reject_unknown_fields(body, {"org_name", "email", "notes"})
    if err:
        return err
    org = str(body.get("org_name") or "").strip()
    email_value = str(body.get("email") or "").strip().lower()
    errors = {}
    if not org:
        errors["org_name"] = "This field is required."
    if not _valid_email(email_value):
        errors["email"] = "Enter a valid email address."
    if len(org) > 200:
        errors["org_name"] = "Must be 200 characters or fewer."
    if len(email_value) > 254:
        errors["email"] = "Must be 254 characters or fewer."
    if errors:
        return error("validation_error", "Validation failed.", 422, errors)
    if Tenant.objects.filter(email__iexact=email_value).exists():
        return error("tenant_conflict", "A tenant with this email already exists.", 409)
    try:
        with transaction.atomic():
            tenant = Tenant.objects.create(
                org_name=org,
                email=email_value,
                notes=str(body.get("notes") or ""),
            )
    except IntegrityError:
        return error("tenant_conflict", "A tenant with this email already exists.", 409)
    return data_response(tenant_data(tenant), status=201)


@vendor_auth(methods={"GET", "PATCH"})
def tenant_detail(request, tenant_id):
    tenant = (
        Tenant.objects.select_related("subscription__plan")
        .prefetch_related("license_keys")
        .filter(pk=tenant_id)
        .first()
    )
    if tenant is None:
        return error("not_found", "Tenant not found.", 404)
    if request.method == "PATCH":
        denied = _permission(request, "tenants_write")
        if denied:
            return denied
        return _update_tenant(request, tenant)
    result = tenant_data(tenant)
    licenses = list(tenant.license_keys.select_related("tenant"))
    invite_qs = (
        InviteCode.objects.select_related("tenant")
        .filter(Q(tenant=tenant) | Q(intended_email__iexact=tenant.email))
        .distinct()
    )
    invites = invite_qs[:10]
    pending = (
        tenant.plan_change_requests.select_related(
            "tenant", "current_plan", "requested_plan", "decided_by"
        )
        .filter(status=PlanChangeRequest.Status.PENDING)
        .first()
    )
    payments = tenant.payments.select_related("tenant", "actor")[:10]
    heartbeat_qs = HeartbeatEvent.objects.select_related("license_key__tenant").filter(
        license_key__tenant=tenant
    )
    heartbeats = heartbeat_qs[:10]
    event_qs = tenant.control_events.select_related("actor", "tenant", "license_key")
    events = event_qs[:10]
    result.update(
        {
            "licenses": {
                "count": tenant.license_keys.count(),
                "results": [license_data(row) for row in licenses],
                "url": f"/api/admin/licenses?tenant={tenant.pk}",
            },
            "invites": {
                "count": invite_qs.count(),
                "results": [invite_data(row) for row in invites],
                "url": f"/api/admin/invites?{urlencode({'q': tenant.email})}",
            },
            "pending_plan_change": plan_change_data(pending) if pending else None,
            "recent_payments": {
                "count": tenant.payments.count(),
                "results": [payment_data(row) for row in payments],
                "url": f"/api/admin/payments?tenant={tenant.pk}",
            },
            "recent_heartbeats": {
                "count": heartbeat_qs.count(),
                "results": [heartbeat_event_data(row) for row in heartbeats],
                "url": f"/api/admin/heartbeats?tenant={tenant.pk}",
            },
            "recent_events": {
                "count": event_qs.count(),
                "results": [control_event_data(row) for row in events],
                "url": f"/api/admin/events?tenant={tenant.pk}",
            },
        }
    )
    return data_response(result)


@idempotent
def _update_tenant(request, tenant):
    body, err = parse_json(request)
    if err:
        return err
    allowed = {"org_name", "email", "notes"}
    forbidden = set(body) - allowed
    if forbidden:
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {key: "This field cannot be changed." for key in forbidden},
        )
    if "email" in body:
        value = str(body["email"]).strip().lower()
        if not _valid_email(value):
            return error(
                "validation_error",
                "Validation failed.",
                422,
                {"email": "Enter a valid email address."},
            )
        if Tenant.objects.exclude(pk=tenant.pk).filter(email__iexact=value).exists():
            return error(
                "tenant_conflict", "A tenant with this email already exists.", 409
            )
        tenant.email = value
    if "org_name" in body:
        tenant.org_name = str(body["org_name"]).strip()
        if not tenant.org_name or len(tenant.org_name) > 200:
            return error(
                "validation_error",
                "Validation failed.",
                422,
                {"org_name": "This field cannot be blank."},
            )
    if "notes" in body:
        tenant.notes = str(body["notes"] or "")
    try:
        with transaction.atomic():
            tenant.save()
    except IntegrityError:
        return error("tenant_conflict", "A tenant with this email already exists.", 409)
    return data_response(tenant_data(tenant))


def _control_tenant(request, tenant_id, target, action):
    body, err = parse_json(request)
    if err:
        return err
    err = _reject_unknown_fields(body, {"reason"})
    if err:
        return err
    reason = body.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 500:
        return _field_error("reason", "Must be a string of 500 characters or fewer.")
    source = (
        LicenseKey.Status.ACTIVE
        if target == LicenseKey.Status.SUSPENDED
        else LicenseKey.Status.SUSPENDED
    )
    with transaction.atomic():
        # Registration takes the same tenant lock before issuing another
        # install. This makes the account kill switch a true boundary: an
        # install cannot be inserted between selecting and updating its keys.
        tenant = Tenant.objects.select_for_update().filter(pk=tenant_id).first()
        if tenant is None:
            return error("not_found", "Tenant not found.", 404)
        rows = list(
            LicenseKey.objects.select_for_update()
            .select_related("tenant")
            .filter(tenant=tenant, status=source)
        )
        LicenseKey.objects.filter(pk__in=[row.pk for row in rows]).update(status=target)
        events = ControlEvent.objects.bulk_create(
            [
                ControlEvent(
                    actor=request.user,
                    action=action,
                    tenant=tenant,
                    license_key=row,
                    metadata={"scope": "tenant", "reason": reason},
                )
                for row in rows
            ]
        )
    for row in rows:
        row.status = target
    return data_response(
        {
            "affected_license_count": len(rows),
            "licenses": [license_data(row) for row in rows],
            "control_event_ids": [event.pk for event in events],
        }
    )


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def tenant_suspend(request, tenant_id):
    return _control_tenant(
        request, tenant_id, LicenseKey.Status.SUSPENDED, ControlEvent.Action.SUSPEND
    )


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def tenant_resume(request, tenant_id):
    return _control_tenant(
        request, tenant_id, LicenseKey.Status.ACTIVE, ControlEvent.Action.RESUME
    )


@vendor_auth(methods={"GET"})
def licenses(request):
    qs = LicenseKey.objects.select_related("tenant").annotate(
        last_seen=Max("heartbeat_events__received_at")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(tenant__org_name__icontains=q)
            | Q(tenant__email__icontains=q)
            | Q(key_prefix__icontains=q)
            | Q(notes__icontains=q)
        )
    status = request.GET.get("status", "").upper()
    if status in LicenseKey.Status.values:
        qs = qs.filter(status=status)
    tenant_id, err = _query_id(request, "tenant")
    if err:
        return err
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    health = request.GET.get("health", "").upper()
    now = timezone.now()
    online_cutoff = now - timedelta(minutes=settings.HEARTBEAT_ONLINE_MINUTES)
    quiet_cutoff = now - timedelta(minutes=settings.HEARTBEAT_DELAYED_MINUTES)
    if health == "ONLINE":
        qs = qs.filter(last_seen__gte=online_cutoff)
    elif health == "DELAYED":
        qs = qs.filter(last_seen__lt=online_cutoff, last_seen__gte=quiet_cutoff)
    elif health == "QUIET":
        qs = qs.filter(Q(last_seen__lt=quiet_cutoff) | Q(last_seen__isnull=True))
    qs = qs.order_by("-created_at")
    if status == "EXPIRED":
        rows = []
        for row in qs.filter(status=LicenseKey.Status.ACTIVE):
            serialized = license_data(row)
            if serialized["computed_status"] == "EXPIRED":
                rows.append(serialized)
        return paginate_list(request, rows)
    return paginate(request, qs, license_data)


@vendor_auth(methods={"GET"})
def license_detail(request, license_id):
    key = LicenseKey.objects.select_related("tenant").filter(pk=license_id).first()
    if key is None:
        return error("not_found", "License not found.", 404)
    result = license_data(key)
    result["recent_heartbeats"] = [
        heartbeat_event_data(row) for row in key.heartbeat_events.all()[:20]
    ]
    result["recent_events"] = [
        control_event_data(row)
        for row in key.control_events.select_related("actor", "tenant", "license_key")[
            :20
        ]
    ]
    return data_response(result)


def _control_license(request, license_id, target, action, *, require_reason=False):
    body, err = parse_json(request)
    if err:
        return err
    err = _reject_unknown_fields(body, {"reason"})
    if err:
        return err
    reason = body.get("reason") or ""
    if not isinstance(reason, str) or len(reason) > 500:
        return _field_error("reason", "Must be a string of 500 characters or fewer.")
    reason = reason.strip()
    if require_reason and not reason:
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"reason": "A confirmation reason is required."},
        )
    with transaction.atomic():
        key = (
            LicenseKey.objects.select_for_update()
            .select_related("tenant")
            .filter(pk=license_id)
            .first()
        )
        if key is None:
            return error("not_found", "License not found.", 404)
        if key.status == LicenseKey.Status.REVOKED:
            return error("license_revoked", "A revoked license cannot be changed.", 409)
        if (
            target == LicenseKey.Status.ACTIVE
            and key.status != LicenseKey.Status.SUSPENDED
        ):
            return error(
                "state_conflict", "Only suspended licenses can be resumed.", 409
            )
        if (
            target == LicenseKey.Status.SUSPENDED
            and key.status != LicenseKey.Status.ACTIVE
        ):
            return error(
                "state_conflict", "Only active licenses can be suspended.", 409
            )
        previous = key.status
        key.status = target
        fields = ["status"]
        if target == LicenseKey.Status.REVOKED:
            key.revoked_at = timezone.now()
            fields.append("revoked_at")
        key.save(update_fields=fields)
        event = ControlEvent.objects.create(
            actor=request.user,
            action=action,
            tenant=key.tenant,
            license_key=key,
            metadata={"from": previous, "to": target, "reason": reason},
        )
    return data_response({"license": license_data(key), "control_event_id": event.pk})


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def license_suspend(request, license_id):
    return _control_license(
        request, license_id, LicenseKey.Status.SUSPENDED, ControlEvent.Action.SUSPEND
    )


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def license_resume(request, license_id):
    return _control_license(
        request, license_id, LicenseKey.Status.ACTIVE, ControlEvent.Action.RESUME
    )


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def license_revoke(request, license_id):
    return _control_license(
        request,
        license_id,
        LicenseKey.Status.REVOKED,
        ControlEvent.Action.REVOKE,
        require_reason=True,
    )


@vendor_auth(permission="licenses.control", methods={"POST", "DELETE"})
@idempotent
def license_message(request, license_id):
    if request.method == "DELETE":
        message = ""
    else:
        body, err = parse_json(request)
        if err:
            return err
        err = _reject_unknown_fields(body, {"message"})
        if err:
            return err
        message = body.get("message") or ""
        if not isinstance(message, str):
            return _field_error("message", "Must be a string.")
        message = message.strip()
        if len(message) > 500:
            return error(
                "validation_error",
                "Validation failed.",
                422,
                {"message": "Must be 500 characters or fewer."},
            )
    with transaction.atomic():
        key = (
            LicenseKey.objects.select_for_update()
            .select_related("tenant")
            .filter(pk=license_id)
            .first()
        )
        if key is None:
            return error("not_found", "License not found.", 404)
        old = key.message
        key.message = message
        key.save(update_fields=["message"])
        event = ControlEvent.objects.create(
            actor=request.user,
            action=ControlEvent.Action.SET_MESSAGE,
            tenant=key.tenant,
            license_key=key,
            metadata={"old": old, "new": message},
        )
    return data_response({"license": license_data(key), "control_event_id": event.pk})


@vendor_auth(permission="licenses.control", methods={"POST"})
@idempotent
def license_bulk_action(request):
    body, err = parse_json(request)
    if err:
        return err
    ids = body.get("ids")
    action = str(body.get("action") or "").upper()
    reason = body.get("reason") or ""
    err = _reject_unknown_fields(body, {"ids", "action", "reason"})
    if err:
        return err
    if not isinstance(reason, str) or len(reason) > 500:
        return _field_error("reason", "Must be a string of 500 characters or fewer.")
    reason = reason.strip()
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"ids": "Provide between 1 and 200 license IDs."},
        )
    if action not in ("SUSPEND", "RESUME"):
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"action": "Use SUSPEND or RESUME."},
        )
    normalized_ids = []
    for value in ids:
        parsed, err = _integer(value, "ids")
        if err:
            return _field_error("ids", "Every license ID must be a positive integer.")
        normalized_ids.append(parsed)
    if len(set(normalized_ids)) != len(normalized_ids):
        return _field_error("ids", "License IDs must not contain duplicates.")
    ids = normalized_ids
    source = (
        LicenseKey.Status.ACTIVE if action == "SUSPEND" else LicenseKey.Status.SUSPENDED
    )
    target = (
        LicenseKey.Status.SUSPENDED if action == "SUSPEND" else LicenseKey.Status.ACTIVE
    )
    event_action = (
        ControlEvent.Action.SUSPEND
        if action == "SUSPEND"
        else ControlEvent.Action.RESUME
    )
    with transaction.atomic():
        rows = list(
            LicenseKey.objects.select_for_update()
            .select_related("tenant")
            .filter(pk__in=ids, status=source)
        )
        missing = set(map(str, ids)) - {str(row.pk) for row in rows}
        if missing:
            return error(
                "state_conflict",
                "Some licenses do not exist or are not eligible for this action.",
                409,
                {"ids": sorted(missing)},
            )
        LicenseKey.objects.filter(pk__in=ids).update(status=target)
        events = ControlEvent.objects.bulk_create(
            [
                ControlEvent(
                    actor=request.user,
                    action=event_action,
                    tenant=row.tenant,
                    license_key=row,
                    metadata={"scope": "bulk", "reason": reason},
                )
                for row in rows
            ]
        )
    for row in rows:
        row.status = target
    return data_response(
        {
            "affected_license_count": len(rows),
            "licenses": [license_data(row) for row in rows],
            "control_event_ids": [row.pk for row in events],
        }
    )


@vendor_auth(methods={"GET", "POST"})
def invites(request):
    if request.method == "POST":
        denied = _permission(request, "invites_manage")
        if denied:
            return denied
        return _create_invite(request)
    qs = InviteCode.objects.select_related("tenant")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(intended_email__icontains=q)
            | Q(intended_org_name__icontains=q)
            | Q(notes__icontains=q)
            | Q(tenant__org_name__icontains=q)
        )
    state = request.GET.get("state", "").upper()
    now = timezone.now()
    if state == "UNUSED":
        qs = qs.filter(consumed_at__isnull=True, revoked_at__isnull=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gte=now)
        )
    elif state == "CONSUMED":
        qs = qs.filter(consumed_at__isnull=False)
    elif state == "EXPIRED":
        qs = qs.filter(
            consumed_at__isnull=True, revoked_at__isnull=True, expires_at__lt=now
        )
    elif state == "REVOKED":
        qs = qs.filter(revoked_at__isnull=False)
    return paginate(request, qs.order_by("-created_at"), invite_data)


@idempotent
def _create_invite(request):
    body, err = parse_json(request)
    if err:
        return err
    err = _reject_unknown_fields(
        body, {"intended_email", "intended_org_name", "expires_at", "notes"}
    )
    if err:
        return err
    expires, err = _date(body.get("expires_at"), "expires_at")
    if err:
        return err
    if expires and expires <= timezone.now():
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"expires_at": "Expiry must be in the future."},
        )
    email_value = str(body.get("intended_email") or "").strip().lower()
    org_name = str(body.get("intended_org_name") or "").strip()
    if email_value and not _valid_email(email_value):
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"intended_email": "Enter a valid email address."},
        )
    if len(org_name) > 200:
        return error(
            "validation_error",
            "Validation failed.",
            422,
            {"intended_org_name": "Must be 200 characters or fewer."},
        )
    with transaction.atomic():
        invite = InviteCode.objects.create(
            intended_email=email_value,
            intended_org_name=org_name,
            expires_at=expires,
            notes=str(body.get("notes") or ""),
        )
        event = ControlEvent.objects.create(
            actor=request.user,
            action=ControlEvent.Action.INVITE_CREATE,
            metadata={"invite_id": invite.pk, "intended_email": invite.intended_email},
        )
    data = invite_data(invite, include_code=True)
    data["control_event_id"] = event.pk
    return data_response(data, status=201)


@vendor_auth(methods={"GET", "DELETE"})
def invite_detail(request, invite_id):
    invite = InviteCode.objects.select_related("tenant").filter(pk=invite_id).first()
    if invite is None:
        return error("not_found", "Invite not found.", 404)
    if request.method == "GET":
        return data_response(invite_data(invite, include_code=True))
    denied = _permission(request, "invites_manage")
    if denied:
        return denied
    return _revoke_invite(request, invite)


@idempotent
def _revoke_invite(request, invite):
    with transaction.atomic():
        invite = InviteCode.objects.select_for_update().get(pk=invite.pk)
        if invite.consumed_at:
            return error("invite_consumed", "Consumed invites cannot be revoked.", 409)
        if invite.revoked_at:
            return error("invite_revoked", "Invite is already revoked.", 409)
        invite.revoked_at = timezone.now()
        invite.revoked_by = request.user
        invite.save(update_fields=["revoked_at", "revoked_by"])
        event = ControlEvent.objects.create(
            actor=request.user,
            action=ControlEvent.Action.INVITE_REVOKE,
            tenant=invite.tenant,
            metadata={"invite_id": invite.pk},
        )
    return data_response({"invite": invite_data(invite), "control_event_id": event.pk})


def _plan_fields(body, plan=None):
    errors = {}
    values = {}
    text_fields = ("code", "name", "description")
    for field in text_fields:
        if field in body:
            values[field] = str(body[field] or "").strip()
    if plan is None:
        for field in ("code", "name", "price"):
            if field not in body or body[field] in ("", None):
                errors[field] = "This field is required."
    if "code" in values:
        import re

        if (
            not values["code"]
            or len(values["code"]) > 32
            or not re.fullmatch(r"[-a-zA-Z0-9_]+", values["code"])
        ):
            errors["code"] = "Use letters, numbers, underscores, or hyphens."
        elif (
            SubscriptionPlan.objects.exclude(pk=plan.pk if plan else None)
            .filter(code=values["code"])
            .exists()
        ):
            errors["code"] = "This code is already in use."
    if "name" in values and (not values["name"] or len(values["name"]) > 120):
        errors["name"] = "Must be between 1 and 120 characters."
    if "price" in body:
        values["price"], price_error = _decimal_amount(body["price"])
        if price_error:
            values.pop("price", None)
            errors["price"] = f"Enter a decimal amount from 0.00 to {MAX_MONEY}."
    for field in ("period_days", "warn_days", "grace_days", "sort_order"):
        if field in body:
            minimum = 1 if field == "period_days" else 0
            values[field], integer_error = _integer(body[field], field, minimum=minimum)
            if integer_error:
                values.pop(field, None)
                errors[field] = (
                    f"Enter an integer from {minimum} to {MAX_POSITIVE_INTEGER}."
                )
    if "is_active" in body:
        if not isinstance(body["is_active"], bool):
            errors["is_active"] = "Enter true or false."
        else:
            values["is_active"] = body["is_active"]
    allowed = set(text_fields) | {
        "price",
        "period_days",
        "warn_days",
        "grace_days",
        "sort_order",
        "is_active",
    }
    for field in set(body) - allowed:
        errors[field] = "Unknown field."
    return values, errors


@vendor_auth(methods={"GET", "POST"})
def plans(request):
    if request.method == "POST":
        denied = _permission(request, "plans_manage")
        if denied:
            return denied
        return _create_plan(request)
    qs = SubscriptionPlan.objects.annotate(subscriber_count=Count("subscriptions"))
    if request.GET.get("include_inactive", "").lower() not in ("true", "1", "yes"):
        qs = qs.filter(is_active=True)
    return paginate(
        request,
        qs,
        lambda row: plan_data(row, subscriber_count=row.subscriber_count),
    )


@idempotent
def _create_plan(request):
    body, err = parse_json(request)
    if err:
        return err
    values, errors = _plan_fields(body)
    if errors:
        return error("validation_error", "Validation failed.", 422, errors)
    try:
        with transaction.atomic():
            plan = SubscriptionPlan.objects.create(**values)
    except IntegrityError:
        return error("plan_conflict", "A plan with this code already exists.", 409)
    return data_response(plan_data(plan, subscriber_count=0), status=201)


@vendor_auth(methods={"PATCH", "DELETE"})
@idempotent
def plan_detail(request, plan_id):
    denied = _permission(request, "plans_manage")
    if denied:
        return denied
    plan = SubscriptionPlan.objects.filter(pk=plan_id).first()
    if plan is None:
        return error("not_found", "Plan not found.", 404)
    if request.method == "DELETE":
        plan.is_active = False
        plan.save(update_fields=["is_active", "updated_at"])
        return data_response(
            plan_data(plan, subscriber_count=plan.subscriptions.count())
        )
    body, err = parse_json(request)
    if err:
        return err
    values, errors = _plan_fields(body, plan=plan)
    if errors:
        return error("validation_error", "Validation failed.", 422, errors)
    for field, value in values.items():
        setattr(plan, field, value)
    try:
        with transaction.atomic():
            plan.save()
    except IntegrityError:
        return error("plan_conflict", "A plan with this code already exists.", 409)
    return data_response(plan_data(plan, subscriber_count=plan.subscriptions.count()))


@vendor_auth(methods={"GET"})
def subscriptions(request):
    qs = Subscription.objects.select_related("tenant", "plan")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(tenant__org_name__icontains=q)
            | Q(tenant__email__icontains=q)
            | Q(plan__name__icontains=q)
        )
    status = request.GET.get("status", "").upper()
    if status in Subscription.Status.values:
        qs = qs.filter(status=status)
    billing_state = request.GET.get("billing_state", "").upper()
    qs = qs.order_by("tenant__org_name")
    if billing_state in ("PAID", "WARNING", "GRACE", "EXPIRED"):
        rows = [subscription_data(row) for row in qs]
        rows = [row for row in rows if row["billing_state"] == billing_state]
        return paginate_list(request, rows)
    return paginate(request, qs, subscription_data)


@vendor_auth(methods={"GET", "PATCH"})
def subscription_detail(request, tenant_id):
    sub = (
        Subscription.objects.select_related("tenant", "plan")
        .filter(tenant_id=tenant_id)
        .first()
    )
    if sub is None:
        return error("not_found", "Subscription not found.", 404)
    if request.method == "GET":
        data = subscription_data(sub)
        changes = PlanChangeRequest.objects.select_related(
            "tenant", "current_plan", "requested_plan", "decided_by"
        ).filter(tenant_id=tenant_id)[:20]
        data["plan_change_requests"] = [plan_change_data(row) for row in changes]
        return data_response(data)
    denied = _permission(request, "subscriptions_write")
    if denied:
        return denied
    return _update_subscription(request, sub)


@idempotent
def _update_subscription(request, sub):
    body, err = parse_json(request)
    if err:
        return err
    forbidden = {
        "paid_through",
        "last_charged_at",
        "last_warn_sent_at",
        "last_grace_sent_at",
        "last_lockout_sent_at",
    } & set(body)
    allowed = {"plan_id", "price", "period_days", "warn_days", "grace_days", "status"}
    unknown = set(body) - allowed - forbidden
    errors = {
        field: "This billing-managed field cannot be changed." for field in forbidden
    }
    errors.update({field: "Unknown field." for field in unknown})
    if errors:
        return error("validation_error", "Validation failed.", 422, errors)
    with transaction.atomic():
        # Billing settlement also locks this row. Taking the same lock keeps a
        # policy edit from racing a renewal that is reading price/period data.
        sub = (
            Subscription.objects.select_for_update()
            .select_related("tenant")
            .get(pk=sub.pk)
        )
        if "plan_id" in body:
            plan_id, err = _integer(body["plan_id"], "plan_id")
            if err:
                return err
            plan = SubscriptionPlan.objects.filter(pk=plan_id).first()
            if plan is None:
                return error(
                    "validation_error",
                    "Validation failed.",
                    422,
                    {"plan_id": "Plan not found."},
                )
            bind_plan_to_subscription(sub, plan)
        if "price" in body:
            price, err = _decimal_amount(body["price"])
            if err:
                return err
            sub.price = price
        for field in ("period_days", "warn_days", "grace_days"):
            if field in body:
                value, err = _integer(
                    body[field], field, minimum=1 if field == "period_days" else 0
                )
                if err:
                    return err
                setattr(sub, field, value)
        if "status" in body:
            if body["status"] not in Subscription.Status.values:
                return error(
                    "validation_error",
                    "Validation failed.",
                    422,
                    {"status": "Use ACTIVE or CANCELED."},
                )
            sub.status = body["status"]
        sub.save()
    return data_response(subscription_data(sub))


@vendor_auth(methods={"GET"})
def plan_changes(request):
    qs = PlanChangeRequest.objects.select_related(
        "tenant", "current_plan", "requested_plan", "decided_by"
    )
    status = request.GET.get("status", "").upper()
    if status in PlanChangeRequest.Status.values:
        qs = qs.filter(status=status)
    return paginate(request, qs.order_by("-requested_at"), plan_change_data)


def _decide_plan_change(request, change_id, target):
    body, err = parse_json(request)
    if err:
        return err
    err = _reject_unknown_fields(body, {"decision_note"})
    if err:
        return err
    note = body.get("decision_note") or ""
    if not isinstance(note, str) or len(note) > 255:
        return _field_error(
            "decision_note", "Must be a string of 255 characters or fewer."
        )
    with transaction.atomic():
        change = (
            PlanChangeRequest.objects.select_for_update()
            # Only join required, non-nullable relations here. PostgreSQL
            # rejects FOR UPDATE on the nullable side of an outer join.
            .select_related("tenant", "requested_plan")
            .filter(pk=change_id)
            .first()
        )
        if change is None:
            return error("not_found", "Plan-change request not found.", 404)
        if change.status != PlanChangeRequest.Status.PENDING:
            return error(
                "plan_change_decided", "This request has already been decided.", 409
            )
        if target == PlanChangeRequest.Status.APPROVED:
            sub = Subscription.objects.select_for_update().filter(
                tenant=change.tenant
            ).first() or Subscription(tenant=change.tenant)
            bind_plan_to_subscription(sub, change.requested_plan)
            sub.save()
            action = ControlEvent.Action.PLAN_CHANGE_APPROVE
        else:
            action = ControlEvent.Action.PLAN_CHANGE_REJECT
        change.status = target
        change.decided_at = timezone.now()
        change.decided_by = request.user
        change.decision_note = note
        change.save(
            update_fields=["status", "decided_at", "decided_by", "decision_note"]
        )
        event = ControlEvent.objects.create(
            actor=request.user,
            action=action,
            tenant=change.tenant,
            metadata={
                "plan_change_id": change.pk,
                "decision_note": note,
                "requested_plan_id": change.requested_plan_id,
            },
        )
    data = plan_change_data(change)
    data["control_event_id"] = event.pk
    return data_response(data)


@vendor_auth(permission="billing.approve", methods={"POST"})
@idempotent
def plan_change_approve(request, change_id):
    return _decide_plan_change(request, change_id, PlanChangeRequest.Status.APPROVED)


@vendor_auth(permission="billing.approve", methods={"POST"})
@idempotent
def plan_change_reject(request, change_id):
    return _decide_plan_change(request, change_id, PlanChangeRequest.Status.REJECTED)


def _filter_payment_dates(request, qs):
    raw_from = request.GET.get("date_from")
    raw_to = request.GET.get("date_to")
    if raw_from and len(raw_from) == 10:
        raw_from = f"{raw_from}T00:00:00Z"
    if raw_to and len(raw_to) == 10:
        raw_to = f"{raw_to}T23:59:59.999999Z"
    date_from, err = _date(raw_from, "date_from")
    if err:
        return None, err
    date_to, err = _date(raw_to, "date_to")
    if err:
        return None, err
    if date_from:
        qs = qs.filter(created_at__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__lte=date_to)
    return qs, None


@vendor_auth(methods={"GET"})
def payments(request):
    qs = Payment.objects.select_related("tenant", "actor")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(tenant__org_name__icontains=q)
            | Q(tenant__email__icontains=q)
            | Q(external_id__icontains=q)
            | Q(note__icontains=q)
        )
    for query_name, field, choices in (
        ("kind", "kind", Payment.Kind.values),
        ("source", "source", Payment.Source.values),
    ):
        value = request.GET.get(query_name, "").upper()
        if value in choices:
            qs = qs.filter(**{field: value})
    tenant_id, err = _query_id(request, "tenant")
    if err:
        return err
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    qs, err = _filter_payment_dates(request, qs)
    if err:
        return err
    return paginate(request, qs.order_by("-created_at"), payment_data)


@vendor_auth(methods={"GET"})
def payment_summary(request):
    days = _window(request.GET.get("window"))
    since = timezone.now() - timedelta(days=days)
    qs = Payment.objects.filter(created_at__gte=since)
    totals = {
        row["kind"]: row["total"]
        for row in qs.values("kind").annotate(total=Sum("amount"))
    }
    sources = {
        row["source"]: row["total"]
        for row in qs.filter(kind=Payment.Kind.TOPUP)
        .values("source")
        .annotate(total=Sum("amount"))
    }
    series = (
        qs.filter(kind=Payment.Kind.TOPUP)
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(amount=Sum("amount"))
        .order_by("date")
    )
    return data_response(
        {
            "window_days": days,
            "topups_total": money(totals.get(Payment.Kind.TOPUP, 0)),
            "charges_total": money(totals.get(Payment.Kind.CHARGE, 0)),
            "adjustments_total": money(totals.get(Payment.Kind.ADJUST, 0)),
            "topups_by_source": {key: money(value) for key, value in sources.items()},
            "series": [
                {"date": str(row["date"]), "amount": money(row["amount"])}
                for row in series
            ],
        }
    )


@vendor_auth(methods={"GET"})
def events(request):
    qs = ControlEvent.objects.select_related("actor", "tenant", "license_key")
    tenant_id, err = _query_id(request, "tenant")
    if err:
        return err
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    license_id, err = _query_id(request, "license")
    if err:
        return err
    if license_id is not None:
        qs = qs.filter(license_key_id=license_id)
    action = request.GET.get("action", "").upper()
    if action in ControlEvent.Action.values:
        qs = qs.filter(action=action)
    actor = request.GET.get("actor", "").strip()
    if actor:
        if actor.isascii() and actor.isdigit():
            actor_id, err = _integer(actor, "actor")
            if err:
                return err
            qs = qs.filter(actor_id=actor_id)
        else:
            qs = qs.filter(actor__username__iexact=actor)
    return paginate(request, qs.order_by("-created_at"), control_event_data)


@vendor_auth(methods={"GET"})
def heartbeats(request):
    qs = HeartbeatEvent.objects.select_related("license_key__tenant")
    tenant_id, err = _query_id(request, "tenant")
    if err:
        return err
    if tenant_id is not None:
        qs = qs.filter(license_key__tenant_id=tenant_id)
    license_id, err = _query_id(request, "license")
    if err:
        return err
    if license_id is not None:
        qs = qs.filter(license_key_id=license_id)
    if request.GET.get("version"):
        qs = qs.filter(client_version__icontains=request.GET["version"])
    health = request.GET.get("health", "").upper()
    now = timezone.now()
    online_cutoff = now - timedelta(minutes=settings.HEARTBEAT_ONLINE_MINUTES)
    quiet_cutoff = now - timedelta(minutes=settings.HEARTBEAT_DELAYED_MINUTES)
    if health == "ONLINE":
        qs = qs.filter(received_at__gte=online_cutoff)
    elif health == "DELAYED":
        qs = qs.filter(received_at__lt=online_cutoff, received_at__gte=quiet_cutoff)
    elif health == "QUIET":
        qs = qs.filter(received_at__lt=quiet_cutoff)
    include_payload = request.GET.get("include_payload", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if include_payload and not _allowed(request, "view_sensitive_heartbeat"):
        return error(
            "permission_denied", "Raw heartbeat payload permission is required.", 403
        )
    return paginate(
        request,
        qs.order_by("-received_at"),
        lambda row: heartbeat_event_data(row, include_payload=include_payload, now=now),
    )


@vendor_auth(methods={"GET"})
def heartbeat_detail(request, heartbeat_id):
    row = (
        HeartbeatEvent.objects.select_related("license_key__tenant")
        .filter(pk=heartbeat_id)
        .first()
    )
    if row is None:
        return error("not_found", "Heartbeat event not found.", 404)
    include_payload = _allowed(request, "view_sensitive_heartbeat")
    return data_response(heartbeat_event_data(row, include_payload=include_payload))


@vendor_auth(methods={"GET"})
def system_status(request):
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False
    last_heartbeat = HeartbeatEvent.objects.order_by("-received_at").first()
    lag = None
    if last_heartbeat:
        lag = max(0, int((timezone.now() - last_heartbeat.received_at).total_seconds()))
    last_run = BillingRun.objects.first()
    return data_response(
        {
            "api_version": "1",
            "server_time": timezone.now().isoformat().replace("+00:00", "Z"),
            "deployment_version": settings.DEPLOYMENT_VERSION or None,
            "database_reachable": database_ok,
            "last_successful_billing_job": (
                last_run.completed_at.isoformat().replace("+00:00", "Z")
                if last_run
                else None
            ),
            "active_plan_count": SubscriptionPlan.objects.filter(
                is_active=True
            ).count(),
            "heartbeat_ingestion_lag_seconds": lag,
            "health_thresholds": health_thresholds(),
        }
    )


@vendor_auth()
def api_not_found(_request, unmatched=""):
    return error("not_found", "Vendor API route not found.", 404)
