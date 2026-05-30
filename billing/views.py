"""Payment-provider webhooks. Both credit a tenant's prepaid wallet via the
shared, idempotent ``billing.services.billing.credit_balance``.

These are machine-to-machine endpoints (the provider's servers call them), so
they're CSRF-exempt and authenticate by provider-specific means: Click verifies
an MD5 signature inside the payload; Payme uses HTTP Basic auth.
"""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing.services import click as click_svc
from billing.services import payme as payme_svc


logger = logging.getLogger(__name__)


def _click_params(request):
    """Click posts form-encoded data; tolerate JSON too."""
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            return {}
    return request.POST.dict()


@csrf_exempt
@require_POST
def click_prepare(request):
    return JsonResponse(click_svc.handle_prepare(_click_params(request)))


@csrf_exempt
@require_POST
def click_complete(request):
    return JsonResponse(click_svc.handle_complete(_click_params(request)))


@csrf_exempt
@require_POST
def payme_endpoint(request):
    """Single JSON-RPC endpoint for Payme. Basic-auth gated."""
    if not payme_svc.check_auth(request.META.get('HTTP_AUTHORIZATION', '')):
        return JsonResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': payme_svc.ERR_INSUFFICIENT_PRIVILEGE,
                      'message': {'ru': 'Insufficient privilege',
                                  'en': 'Insufficient privilege',
                                  'uz': 'Insufficient privilege'}},
        }, status=200)

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return JsonResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32700, 'message': {'en': 'Parse error',
                                                  'ru': 'Parse error',
                                                  'uz': 'Parse error'}},
        }, status=200)

    return JsonResponse(payme_svc.dispatch(payload))
