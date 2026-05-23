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

from licenses.models import LicenseKey
from tenants.models import InviteCode, Tenant


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
