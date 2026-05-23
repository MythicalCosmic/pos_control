"""Public-facing endpoints under /api/v1/.

For this first commit only `register` is wired up. Heartbeat lives in
the next commit; revoke-check is optional and deferred."""
import json
import logging

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from licenses.models import HeartbeatEvent, LicenseKey
from tenants.models import InviteCode, Tenant


# Default heartbeat cadence the server suggests to the client. The client
# may pick its own interval anyway; this is advisory. 300s mirrors the
# alpha_pos LICENSE_HEARTBEAT_INTERVAL default.
DEFAULT_NEXT_HEARTBEAT_S = 300


logger = logging.getLogger(__name__)


def _parse_body(request):
    try:
        return json.loads(request.body), None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse(
            {'success': False, 'message': 'Invalid JSON body'}, status=400,
        )


@csrf_exempt
@require_POST
def register(request):
    """Exchange an invite code for a fresh license key.

    The customer (running the alpha_pos setup wizard) POSTs:
        { "email": "...", "org_name": "...", "invite_code": "..." }

    On success returns:
        { "success": true,
          "key": "<48-byte url-safe>",
          "tenant_id": int,
          "expires_at": ISO8601 or null,
          "issued_at": ISO8601 }

    The key is returned EXACTLY ONCE — we keep only its sha256. If the
    customer loses it, staff revokes the row and issues a new code +
    key. There is no recovery."""
    data, err = _parse_body(request)
    if err:
        return err

    email = (data.get('email') or '').strip().lower()
    org_name = (data.get('org_name') or '').strip()
    invite_code = (data.get('invite_code') or '').strip()

    missing = [
        name for name, value in (
            ('email', email), ('org_name', org_name), ('invite_code', invite_code),
        ) if not value
    ]
    if missing:
        return JsonResponse(
            {'success': False, 'message': 'Missing required fields',
             'errors': {f: f'{f} is required' for f in missing}},
            status=422,
        )

    # Wrap the entire redemption in a transaction so a crash between
    # consuming the invite and issuing the key can't half-burn the
    # invite. select_for_update on the invite row prevents two concurrent
    # POSTs from both reading "unconsumed" and both issuing keys.
    try:
        with transaction.atomic():
            try:
                invite = (
                    InviteCode.objects.select_for_update()
                    .get(code=invite_code)
                )
            except InviteCode.DoesNotExist:
                return JsonResponse(
                    {'success': False, 'message': 'Unknown invite code'},
                    status=404,
                )

            if invite.is_consumed():
                return JsonResponse(
                    {'success': False,
                     'message': 'This invite code has already been used'},
                    status=409,
                )
            if invite.is_expired():
                return JsonResponse(
                    {'success': False,
                     'message': 'This invite code has expired'},
                    status=410,
                )

            # If staff pre-bound the invite to a specific email/org, the
            # wizard's payload must match. Case-insensitive on both
            # sides to forgive typos like "Cafe" vs "cafe".
            if invite.intended_email and invite.intended_email.lower() != email:
                return JsonResponse(
                    {'success': False,
                     'message': 'This invite is bound to a different email'},
                    status=403,
                )
            if (invite.intended_org_name
                    and invite.intended_org_name.lower() != org_name.lower()):
                return JsonResponse(
                    {'success': False,
                     'message': 'This invite is bound to a different organization name'},
                    status=403,
                )

            # Reuse the tenant row if it already exists (e.g. an earlier
            # invite was redeemed; this is a rotation). Otherwise create.
            try:
                tenant = Tenant.objects.get(email=email)
                # Cheap update so the dashboard reflects whatever name
                # the customer just typed.
                if org_name and tenant.org_name != org_name:
                    tenant.org_name = org_name
                    tenant.save(update_fields=['org_name'])
            except Tenant.DoesNotExist:
                try:
                    tenant = Tenant.objects.create(
                        org_name=org_name, email=email,
                    )
                except IntegrityError:
                    # Race: another /register call created the tenant
                    # between our get and our create. Re-fetch.
                    tenant = Tenant.objects.get(email=email)

            license_key, cleartext = LicenseKey.issue(tenant)

            invite.tenant = tenant
            invite.consumed_at = timezone.now()
            invite.save(update_fields=['tenant', 'consumed_at'])

    except Exception:
        logger.exception('register failed for email=%s', email)
        return JsonResponse(
            {'success': False, 'message': 'Registration failed; please retry'},
            status=500,
        )

    return JsonResponse({
        'success': True,
        'key': cleartext,
        'tenant_id': tenant.id,
        'expires_at': (
            license_key.expires_at.isoformat() if license_key.expires_at else None
        ),
        'issued_at': license_key.created_at.isoformat(),
    }, status=201)


def _bearer(request):
    """Pull the bearer token from the Authorization header. Returns the
    raw token string, or None if missing / malformed."""
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth.lower().startswith('bearer '):
        return None
    return auth[7:].strip() or None


def _client_ip(request):
    """Best-effort client IP. We're behind a reverse proxy in production
    so honor X-Forwarded-For when present (the proxy must scrub spoofed
    headers; if it doesn't, this is operator-trusted)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


@csrf_exempt
@require_POST
def heartbeat(request):
    """Bearer-authenticated phone-home. The alpha_pos heartbeat daemon
    POSTs here every ~5 minutes.

    Request: Authorization: Bearer <key>, JSON body with client_version,
    branch_id, fingerprint, sent_at, metrics.

    Response 200:
        { "status": "active|suspended|expired",
          "expires_at": ISO8601 or null,
          "server_now": ISO8601,
          "next_heartbeat_in_s": int,
          "message": str or null,
          "ack_id": uuid-string }

    401 on bad / missing key. 410 on revoked.
    """
    token = _bearer(request)
    if not token:
        return JsonResponse(
            {'success': False, 'message': 'Bearer token required'},
            status=401,
        )

    key_row = LicenseKey.lookup_by_cleartext(token)
    if key_row is None:
        return JsonResponse(
            {'success': False, 'message': 'Unknown license key'},
            status=401,
        )

    # Body is informational only — the server's decision is based on
    # key_row.status / expires_at, not on what the client sends. Tolerate
    # an empty body so clients can heartbeat with no metadata.
    try:
        body = json.loads(request.body) if request.body else {}
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    server_now = timezone.now()
    computed = key_row.computed_status(now=server_now)

    if computed == LicenseKey.Status.REVOKED:
        # 410 Gone — the key was permanently retired. The client should
        # surface a "contact vendor for a new key" message; future
        # heartbeats with the same token will keep getting 410.
        return JsonResponse(
            {'success': False, 'message': 'This license key has been revoked',
             'status': 'REVOKED'},
            status=410,
        )

    # Record the event AFTER we know it's not a bogus token but BEFORE
    # responding. The ack_id round-trips so the client can correlate.
    event = HeartbeatEvent.objects.create(
        license_key=key_row,
        ip=_client_ip(request),
        client_version=str(body.get('client_version', ''))[:120],
        branch_id=str(body.get('branch_id', ''))[:120],
        fingerprint=str(body.get('fingerprint', ''))[:128],
        payload=body,
    )

    return JsonResponse({
        'success': True,
        'status': computed,
        'expires_at': key_row.expires_at.isoformat() if key_row.expires_at else None,
        'server_now': server_now.isoformat(),
        'next_heartbeat_in_s': DEFAULT_NEXT_HEARTBEAT_S,
        'message': key_row.message or None,
        'ack_id': str(event.ack_id),
    })
