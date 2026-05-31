"""Public-facing endpoints under /api/v1/.

For this first commit only `register` is wired up. Heartbeat lives in
the next commit; revoke-check is optional and deferred."""
import hashlib
import hmac
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

# Hard cap on heartbeat / register body size to keep a misbehaving client
# from posting a 50 MB JSON blob that the JSON parser would happily try to
# eat. Real heartbeats are < 1 KB; allow 8 KB of slack for metrics growth.
MAX_BODY_BYTES = 8 * 1024


logger = logging.getLogger(__name__)


def _locate_invite(*, invite_code, email, now):
    """Return the InviteCode row a /register call should consume, OR a
    JsonResponse describing why it can't.

    Two paths:
    - ``invite_code`` was sent → find that exact code (legacy: customer types
      the code on a card the vendor mailed them).
    - ``invite_code`` blank → find an unconsumed, unexpired invite whose
      ``intended_email`` matches the requester's email. This is the modern
      "email-only" wizard flow: the vendor verifies the email by pre-issuing
      an invite bound to it.

    Either way the row is taken under ``select_for_update`` so concurrent
    /register POSTs can't both claim the same invite.
    """
    if invite_code:
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
        if invite.is_expired(now=now):
            return JsonResponse(
                {'success': False,
                 'message': 'This invite code has expired'},
                status=410,
            )
        if invite.intended_email and invite.intended_email.lower() != email:
            return JsonResponse(
                {'success': False,
                 'message': 'This invite is bound to a different email'},
                status=403,
            )
        return invite

    # Email-only path: vendor pre-issued an invite bound to this address.
    # Picks the oldest unconsumed, unexpired one so re-issuing after a botched
    # setup is just "issue a fresh invite, customer retries". No invite ==
    # vendor hasn't verified this email yet → 403.
    candidates = (
        InviteCode.objects.select_for_update()
        .filter(consumed_at__isnull=True, intended_email__iexact=email)
        .order_by('created_at')
    )
    for invite in candidates:
        if invite.is_expired(now=now):
            continue
        return invite

    return JsonResponse(
        {'success': False,
         'message': ('No pending invite for this email. '
                     'Contact the vendor to be issued one.')},
        status=403,
    )


def _parse_body(request):
    if len(request.body) > MAX_BODY_BYTES:
        return None, JsonResponse(
            {'success': False, 'message': 'Request body too large'}, status=413,
        )
    try:
        return json.loads(request.body), None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse(
            {'success': False, 'message': 'Invalid JSON body'}, status=400,
        )


def _serialize_plan(plan):
    """Wire shape of a SubscriptionPlan. Same fields whether listed by
    /api/v1/plans or attached to the heartbeat response — the alpha_pos
    renderer parses one shape."""
    return {
        'id': plan.pk,
        'code': plan.code,
        'name': plan.name,
        'description': plan.description,
        'price': str(plan.price),
        'period_days': plan.period_days,
        'warn_days': plan.warn_days,
        'grace_days': plan.grace_days,
        'sort_order': plan.sort_order,
    }


@csrf_exempt  # public-read; CSRF would otherwise hide the 405 behind a 403 HTML page
def plans(request):
    """List active subscription plans. No auth — the setup wizard hits
    this to show the customer their choices before they have a license
    key. ``GET`` only; safe to cache aggressively on the alpha_pos side."""
    if request.method != 'GET':
        return JsonResponse(
            {'success': False, 'message': 'GET only'}, status=405,
        )
    from billing.models import SubscriptionPlan
    rows = SubscriptionPlan.objects.filter(is_active=True)
    return JsonResponse({
        'success': True,
        'plans': [_serialize_plan(p) for p in rows],
    })


@csrf_exempt
@require_POST
def register(request):
    """Exchange an emailed invite for a fresh license key.

    The customer (running the alpha_pos setup wizard) POSTs:
        { "email": "..." }
    The vendor "verifies the email" by having pre-issued an ``InviteCode``
    with ``intended_email`` equal to that address. There is no self-serve
    sign-up: if no matching unconsumed invite exists, registration fails.

    Operators can still hand a customer a raw ``invite_code`` (e.g. for a
    bring-your-own-email migration); pass it in the body alongside ``email``
    and the lookup uses the code directly. ``org_name`` is optional and just
    pre-fills the dashboard display.

    On success returns:
        { "success": true,
          "key": "<48-byte url-safe>",
          "tenant_id": int,
          "expires_at": ISO8601 or null,
          "issued_at": ISO8601 }

    The key is returned EXACTLY ONCE — we keep only its sha256. If the
    customer loses it, staff revokes the row and issues a new code + key.
    There is no recovery."""
    data, err = _parse_body(request)
    if err:
        return err

    email = (data.get('email') or '').strip().lower()
    org_name = (data.get('org_name') or '').strip()
    invite_code = (data.get('invite_code') or '').strip()
    plan_id = data.get('plan_id')

    if not email:
        return JsonResponse(
            {'success': False, 'message': 'Missing required fields',
             'errors': {'email': 'email is required'}},
            status=422,
        )

    # The plan is optional at register time so a vendor can issue an
    # invite first and assign a plan later. When the wizard does send
    # one, validate it exists + is active before we touch the invite.
    chosen_plan = None
    if plan_id is not None:
        from billing.models import SubscriptionPlan
        try:
            chosen_plan = SubscriptionPlan.objects.get(
                pk=int(plan_id), is_active=True,
            )
        except (SubscriptionPlan.DoesNotExist, ValueError, TypeError):
            return JsonResponse(
                {'success': False,
                 'message': 'Unknown or inactive subscription plan'},
                status=422,
            )

    now = timezone.now()

    # Wrap the entire redemption in a transaction so a crash between
    # consuming the invite and issuing the key can't half-burn the
    # invite. select_for_update on the invite row prevents two concurrent
    # POSTs from both reading "unconsumed" and both issuing keys.
    try:
        with transaction.atomic():
            invite = _locate_invite(
                invite_code=invite_code, email=email, now=now,
            )
            if isinstance(invite, JsonResponse):
                return invite

            if (invite.intended_org_name and org_name
                    and invite.intended_org_name.lower() != org_name.lower()):
                return JsonResponse(
                    {'success': False,
                     'message': 'This invite is bound to a different organization name'},
                    status=403,
                )

            # Tenant.org_name is required by schema — fall back to the
            # invite's pre-bound name, then to the email's local-part, so
            # an email-only setup wizard never trips the model constraint.
            tenant_org = (
                org_name
                or invite.intended_org_name
                or email.split('@', 1)[0]
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
                        org_name=tenant_org, email=email,
                    )
                except IntegrityError:
                    # Race: another /register call created the tenant
                    # between our get and our create. Re-fetch.
                    tenant = Tenant.objects.get(email=email)

            license_key, cleartext = LicenseKey.issue(tenant)

            # Give the tenant a subscription so the dashboard shows it and the
            # heartbeat has something to settle. If the wizard sent a plan,
            # bind that plan's pricing immediately (so the customer can start
            # topping up to fund it). Otherwise default to a free plan
            # (price=0 → always ACTIVE) until the vendor sets a real price.
            from billing.models import Subscription
            from billing.admin import _bind_plan_to_subscription
            sub, created = Subscription.objects.get_or_create(tenant=tenant)
            if chosen_plan is not None and (created or sub.plan_id is None):
                _bind_plan_to_subscription(sub, chosen_plan)
                sub.save()

            invite.tenant = tenant
            invite.consumed_at = now
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
    # an empty body so clients can heartbeat with no metadata. Oversized
    # bodies short-circuit before json.loads so the parser never tries to
    # eat a multi-megabyte blob.
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse(
            {'success': False, 'message': 'Request body too large'}, status=413,
        )
    try:
        body = json.loads(request.body) if request.body else {}
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    server_now = timezone.now()

    if key_row.status == LicenseKey.Status.REVOKED:
        # 410 Gone — the key was permanently retired. The client should
        # surface a "contact vendor for a new key" message; future
        # heartbeats with the same token will keep getting 410.
        return JsonResponse(
            {'success': False, 'message': 'This license key has been revoked',
             'status': 'REVOKED'},
            status=410,
        )

    # Settle the subscription (charges a period from the prepaid balance if
    # one is due and affordable) and read the money-driven verdict + the
    # numbers the POS shows the operator. A manually SUSPENDED tenant is
    # paused, NOT billed — pass charge=False so we don't debit a customer
    # we've cut off.
    from billing.services.billing import resolve
    suspended = key_row.status == LicenseKey.Status.SUSPENDED
    billing = resolve(key_row.tenant, now=server_now, charge=not suspended)

    # A manual admin SUSPEND still wins over "paid up" — it's the vendor's
    # override kill switch independent of balance.
    if suspended:
        status = LicenseKey.Status.SUSPENDED
    else:
        status = billing.status  # 'ACTIVE' or 'EXPIRED'

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

    # Plan + pending plan-change snapshot for the POS UI. Both can be null
    # (legacy tenants with no plan, or no outstanding request).
    from billing.models import PlanChangeRequest, Subscription
    sub = Subscription.objects.select_related('plan').filter(
        tenant_id=key_row.tenant_id,
    ).first()
    plan_payload = (
        _serialize_plan(sub.plan) if (sub and sub.plan) else None
    )
    pending = PlanChangeRequest.objects.select_related('requested_plan').filter(
        tenant_id=key_row.tenant_id,
        status=PlanChangeRequest.Status.PENDING,
    ).first()
    pending_payload = (
        {
            'id': pending.pk,
            'requested_plan': _serialize_plan(pending.requested_plan),
            'requested_at': pending.requested_at.isoformat(),
        }
        if pending else None
    )

    body = {
        'success': True,
        'status': status,
        # expires_at now carries the subscription's paid-through moment.
        'expires_at': billing.paid_through.isoformat() if billing.paid_through else None,
        'server_now': server_now.isoformat(),
        'next_heartbeat_in_s': DEFAULT_NEXT_HEARTBEAT_S,
        'message': key_row.message or None,
        'ack_id': str(event.ack_id),
        # Prepaid-billing fields the POS uses for the "X days left" banner.
        # `in_grace` flags the soft-cushion state after a missed renewal — the
        # status stays ACTIVE but the POS should display "pay now" prominently.
        'balance': str(billing.balance),
        'days_remaining': billing.days_remaining,
        'warn': billing.warn,
        'in_grace': billing.in_grace,
        # Plan + pending plan-change. Renderer shows the current plan name
        # and (if non-null) "Plan change pending vendor approval" badge.
        'plan': plan_payload,
        'pending_plan_change': pending_payload,
    }
    # Sign the response body so a MITM that's bypassed TLS still can't forge a
    # "status: ACTIVE" reply. Key is the bearer license key itself — both
    # sides already share it, and rotating the key invalidates old signatures
    # for free. alpha_pos verifies in licensing/services/heartbeat.py.
    raw = json.dumps(body, separators=(',', ':'), sort_keys=True).encode('utf-8')
    signature = hmac.new(token.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    response = JsonResponse(body)
    response['X-Response-Signature'] = f'sha256={signature}'
    return response


@csrf_exempt
@require_POST
def plan_change(request):
    """Bearer-authed: file a request to move to a different plan.

    POST body: ``{"plan_id": <int>, "note": "<optional reason>"}``.
    The customer authenticates with the same license key they use for
    /heartbeat. The request is queued for vendor approval — billing
    does NOT change here. The vendor approves in the admin and the swap
    takes effect at the next charge.

    Returns 201 on a fresh request; 200 if there's already a pending
    request for this tenant (idempotent, returns the existing row);
    422 on bad plan; 409 if asking for the plan they're already on.
    """
    token = _bearer(request)
    if not token:
        return JsonResponse(
            {'success': False, 'message': 'Bearer token required'},
            status=401,
        )
    key_row = LicenseKey.lookup_by_cleartext(token)
    if key_row is None or key_row.status == LicenseKey.Status.REVOKED:
        return JsonResponse(
            {'success': False, 'message': 'Invalid license key'},
            status=401,
        )

    data, err = _parse_body(request)
    if err:
        return err

    plan_id = data.get('plan_id')
    note = (data.get('note') or '')[:255]

    from billing.models import PlanChangeRequest, Subscription, SubscriptionPlan
    try:
        requested_plan = SubscriptionPlan.objects.get(
            pk=int(plan_id), is_active=True,
        )
    except (SubscriptionPlan.DoesNotExist, ValueError, TypeError):
        return JsonResponse(
            {'success': False, 'message': 'Unknown or inactive plan'},
            status=422,
        )

    sub = Subscription.objects.select_related('plan').filter(
        tenant_id=key_row.tenant_id,
    ).first()
    if sub and sub.plan_id == requested_plan.pk:
        return JsonResponse(
            {'success': False,
             'message': 'You are already on this plan'},
            status=409,
        )

    # One PENDING per tenant — repeat POSTs (network blip, customer
    # hammering the button) collapse to the same row. We can't lock a
    # row that doesn't exist yet, so we race-detect via the partial
    # unique constraint: two concurrent creates → the loser sees
    # IntegrityError and returns the winner's row as 200.
    def _existing_or_none():
        return (
            PlanChangeRequest.objects
            .filter(tenant_id=key_row.tenant_id,
                    status=PlanChangeRequest.Status.PENDING)
            .first()
        )

    existing = _existing_or_none()
    if existing is not None:
        return JsonResponse({
            'success': True,
            'status': existing.status,
            'request_id': existing.pk,
            'requested_plan': _serialize_plan(existing.requested_plan),
            'message': 'A plan change is already pending vendor approval.',
        }, status=200)

    try:
        with transaction.atomic():
            req = PlanChangeRequest.objects.create(
                tenant_id=key_row.tenant_id,
                current_plan=sub.plan if sub else None,
                requested_plan=requested_plan,
                note=note,
            )
    except IntegrityError:
        # Another concurrent POST just won — return their row, not 500.
        winner = _existing_or_none()
        if winner is None:
            # The PENDING they created got APPROVED/REJECTED between our
            # create and our re-query — exceedingly rare; treat as success
            # but unusable: the customer can retry.
            return JsonResponse({
                'success': False,
                'message': 'Concurrent change resolved; please retry.',
            }, status=409)
        return JsonResponse({
            'success': True,
            'status': winner.status,
            'request_id': winner.pk,
            'requested_plan': _serialize_plan(winner.requested_plan),
            'message': 'A plan change is already pending vendor approval.',
        }, status=200)

    return JsonResponse({
        'success': True,
        'status': req.status,
        'request_id': req.pk,
        'requested_plan': _serialize_plan(requested_plan),
        'message': 'Plan change submitted for vendor approval.',
    }, status=201)
