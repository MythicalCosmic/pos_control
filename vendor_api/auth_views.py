import ipaddress
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from vendor_api.auth import (
    data_response,
    error,
    idempotent,
    issue_session,
    parse_json,
    rotate_session,
    token_hash,
    user_data,
    vendor_auth,
)
from vendor_api.models import LoginAttempt, VendorSession


def _ip(request):
    candidate = None
    if settings.TRUST_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # The trusted edge proxy appends/replaces the right-most address.
            candidate = forwarded.rsplit(",", 1)[-1].strip()
    candidate = candidate or request.META.get("REMOTE_ADDR")
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


@csrf_exempt
def login(request):
    if request.method != "POST":
        return error("method_not_allowed", "POST required.", 405)
    body, err = parse_json(request)
    if err:
        return err
    username = str(body.get("username") or "").strip()[:150]
    password = str(body.get("password") or "")
    ip = _ip(request)
    since = timezone.now() - timedelta(seconds=settings.VENDOR_LOGIN_WINDOW_SECONDS)
    identity = Q(username__iexact=username)
    if ip:
        identity |= Q(ip=ip)
    recent_failures = LoginAttempt.objects.filter(
        identity,
        success=False,
        created_at__gte=since,
    ).count()
    if recent_failures >= settings.VENDOR_LOGIN_MAX_ATTEMPTS:
        LoginAttempt.objects.create(username=username, ip=ip, success=False)
        response = error("login_rate_limited", "Too many failed login attempts.", 429)
        response["Retry-After"] = str(settings.VENDOR_LOGIN_WINDOW_SECONDS)
        return response
    user = authenticate(request, username=username, password=password)
    allowed = bool(user and user.is_active and user.is_staff)
    LoginAttempt.objects.create(username=username, ip=ip, success=allowed)
    if not allowed:
        return error("invalid_credentials", "Invalid vendor credentials.", 401)
    _, access, refresh = issue_session(user)
    return data_response(
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": settings.VENDOR_ACCESS_TOKEN_SECONDS,
            "user": user_data(user),
        }
    )


@csrf_exempt
def refresh(request):
    if request.method != "POST":
        return error("method_not_allowed", "POST required.", 405)
    body, err = parse_json(request)
    if err:
        return err
    token = str(body.get("refresh_token") or "")
    if not token:
        return error(
            "refresh_token_required",
            "refresh_token is required.",
            422,
            {"refresh_token": "This field is required."},
        )
    with transaction.atomic():
        row = (
            VendorSession.objects.select_for_update()
            .select_related("user")
            .filter(
                refresh_hash=token_hash(token),
                revoked_at__isnull=True,
                refresh_expires_at__gt=timezone.now(),
            )
            .first()
        )
        if row is None or not row.user.is_active or not row.user.is_staff:
            return error(
                "invalid_refresh_token", "Refresh token is invalid or expired.", 401
            )
        access, refresh_token = rotate_session(row)
    return data_response(
        {
            "access_token": access,
            "refresh_token": refresh_token,
            "expires_in": settings.VENDOR_ACCESS_TOKEN_SECONDS,
            "user": user_data(row.user),
        }
    )


@vendor_auth(methods={"GET"})
def me(request):
    return data_response(user_data(request.user))


@vendor_auth(methods={"POST"})
@idempotent
def logout(request):
    request.vendor_session.revoked_at = timezone.now()
    request.vendor_session.save(update_fields=["revoked_at"])
    return data_response({"logged_out": True})
