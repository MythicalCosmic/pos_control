import hashlib
import json
import secrets
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from vendor_api.models import IdempotencyRecord, VendorSession


PERMISSIONS = (
    "tenants.write",
    "licenses.control",
    "billing.approve",
    "invites.manage",
    "plans.manage",
    "subscriptions.write",
    "heartbeats.sensitive",
)
DJANGO_PERMISSION = {
    "tenants.write": "vendor_api.tenants_write",
    "licenses.control": "vendor_api.licenses_control",
    "billing.approve": "vendor_api.billing_approve",
    "invites.manage": "vendor_api.invites_manage",
    "plans.manage": "vendor_api.plans_manage",
    "subscriptions.write": "vendor_api.subscriptions_write",
    "heartbeats.sensitive": "vendor_api.view_sensitive_heartbeat",
}


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(user):
    now = timezone.now()
    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(64)
    row = VendorSession.objects.create(
        user=user,
        access_hash=token_hash(access),
        refresh_hash=token_hash(refresh),
        access_expires_at=now + timedelta(seconds=settings.VENDOR_ACCESS_TOKEN_SECONDS),
        refresh_expires_at=now
        + timedelta(seconds=settings.VENDOR_REFRESH_TOKEN_SECONDS),
    )
    return row, access, refresh


def rotate_session(row):
    now = timezone.now()
    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(64)
    row.access_hash = token_hash(access)
    row.refresh_hash = token_hash(refresh)
    row.access_expires_at = now + timedelta(
        seconds=settings.VENDOR_ACCESS_TOKEN_SECONDS
    )
    row.refresh_expires_at = now + timedelta(
        seconds=settings.VENDOR_REFRESH_TOKEN_SECONDS
    )
    row.save(
        update_fields=[
            "access_hash",
            "refresh_hash",
            "access_expires_at",
            "refresh_expires_at",
            "last_refreshed_at",
        ]
    )
    return access, refresh


def user_permissions(user):
    if user.is_superuser:
        return list(PERMISSIONS)
    return [
        name
        for name, django_name in DJANGO_PERMISSION.items()
        if user.has_perm(django_name)
    ]


def user_data(user):
    name = user.get_full_name().strip() or user.username
    return {
        "id": user.pk,
        "name": name,
        "username": user.username,
        "email": user.email,
        "is_superuser": user.is_superuser,
        "permissions": user_permissions(user),
    }


def error(code, message, status, errors=None):
    body = {"success": False, "code": code, "message": message}
    if errors:
        body["errors"] = errors
    return json_response(body, status=status)


def data_response(data, status=200):
    return json_response({"success": True, "data": data}, status=status)


def json_response(body, *, status=200):
    """Return sensitive vendor JSON with explicit anti-caching headers."""
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def parse_json(request, *, max_bytes=64 * 1024):
    if len(request.body) > max_bytes:
        return None, error("body_too_large", "Request body is too large.", 413)
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return None, error("invalid_json", "Request body must be valid JSON.", 400)
    if not isinstance(data, dict):
        return None, error("invalid_json", "Request body must be a JSON object.", 400)
    return data, None


def _bearer(request):
    value = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def vendor_auth(*, permission=None, methods=None):
    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if methods and request.method not in methods:
                return error("method_not_allowed", "Method not allowed.", 405)
            token = _bearer(request)
            if not token:
                return error("authentication_required", "Bearer token required.", 401)
            now = timezone.now()
            row = (
                VendorSession.objects.select_related("user")
                .filter(
                    access_hash=token_hash(token),
                    revoked_at__isnull=True,
                    access_expires_at__gt=now,
                )
                .first()
            )
            if row is None or not row.user.is_active or not row.user.is_staff:
                return error(
                    "invalid_access_token", "Access token is invalid or expired.", 401
                )
            if permission and not (
                row.user.is_superuser
                or row.user.has_perm(DJANGO_PERMISSION[permission])
            ):
                return error("permission_denied", "Insufficient staff permission.", 403)
            request.vendor_session = row
            request.user = row.user
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def idempotent(view):
    """Replay a completed authenticated mutation when Idempotency-Key repeats."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        key = request.META.get("HTTP_IDEMPOTENCY_KEY", "").strip()
        if not key:
            return view(request, *args, **kwargs)
        if len(key) > 255:
            return error("invalid_idempotency_key", "Idempotency-Key is too long.", 422)
        digest = hashlib.sha256(request.body).hexdigest()
        existing = IdempotencyRecord.objects.filter(user=request.user, key=key).first()
        if existing:
            if (existing.method, existing.path, existing.request_hash) != (
                request.method,
                request.path,
                digest,
            ):
                return error(
                    "idempotency_conflict",
                    "Idempotency-Key was already used for a different request.",
                    409,
                )
            if existing.response_body is None:
                return error(
                    "request_in_progress",
                    "A request with this Idempotency-Key is still in progress.",
                    409,
                )
            return json_response(
                existing.response_body, status=existing.response_status
            )
        with transaction.atomic():
            try:
                # The nested savepoint scopes IntegrityError handling to the
                # claim insert. Business-mutation integrity failures must
                # propagate and roll back the entire action.
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        user=request.user,
                        key=key,
                        method=request.method,
                        path=request.path,
                        request_hash=digest,
                        response_status=None,
                        response_body=None,
                    )
            except IntegrityError:
                existing = IdempotencyRecord.objects.get(user=request.user, key=key)
                if (existing.method, existing.path, existing.request_hash) != (
                    request.method,
                    request.path,
                    digest,
                ):
                    return error(
                        "idempotency_conflict",
                        "Idempotency-Key was already used for a different request.",
                        409,
                    )
                if existing.response_body is None:
                    return error(
                        "request_in_progress",
                        "A request with this Idempotency-Key is still in progress.",
                        409,
                    )
                return json_response(
                    existing.response_body, status=existing.response_status
                )

            response = view(request, *args, **kwargs)
            if not isinstance(response, JsonResponse):
                raise TypeError("Idempotent vendor mutations must return JsonResponse")
            body = json.loads(response.content)
            record.response_status = response.status_code
            record.response_body = body
            record.save(update_fields=["response_status", "response_body"])
            return response

    return wrapped
