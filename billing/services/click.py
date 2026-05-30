"""Click.uz Merchant API (Prepare / Complete) → top up a tenant's wallet.

Click calls two webhooks during a payment:

1. **Prepare** (action=0): "a customer wants to pay merchant_trans_id this
   amount — is that OK?" We verify the signature, map merchant_trans_id to a
   Tenant, and ack.
2. **Complete** (action=1): "the money moved." We verify again and credit the
   wallet via ``billing.services.billing.credit_balance`` (idempotent on the
   Click transaction id, so a retried callback can't double-credit).

``merchant_trans_id`` is the tenant's primary key — that's the reference the
vendor embeds in the customer's payment link.

Signatures are MD5 over a fixed field order plus the merchant secret
(``CLICK_SECRET_KEY``). All amounts are in so'm (UZS).

Docs: https://docs.click.uz/en/click-api/ (Merchant API / SHOP-API).
"""
import hashlib
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings

from django.utils import timezone

from billing.models import ClickTransaction, Payment
from billing.services.billing import credit_balance
from tenants.models import Tenant


logger = logging.getLogger(__name__)


# Click protocol error codes (subset we use).
ERR_OK = 0
ERR_SIGN_CHECK_FAILED = -1
ERR_INCORRECT_AMOUNT = -2
ERR_ACTION_NOT_FOUND = -3
ERR_USER_NOT_FOUND = -5
ERR_TRANSACTION_CANCELLED = -9

ACTION_PREPARE = '0'
ACTION_COMPLETE = '1'


def _expected_sign(params, *, with_prepare_id: bool) -> str:
    """Rebuild the MD5 sign string Click sends. Field order is fixed by the
    Click spec; Complete inserts merchant_prepare_id before amount."""
    secret = settings.CLICK_SECRET_KEY
    parts = [
        params.get('click_trans_id', ''),
        params.get('service_id', ''),
        secret,
        params.get('merchant_trans_id', ''),
    ]
    if with_prepare_id:
        parts.append(params.get('merchant_prepare_id', ''))
    parts += [
        params.get('amount', ''),
        params.get('action', ''),
        params.get('sign_time', ''),
    ]
    raw = ''.join(str(p) for p in parts)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _signature_ok(params, *, with_prepare_id: bool) -> bool:
    from django.utils.crypto import constant_time_compare
    sent = params.get('sign_string', '') or ''
    return constant_time_compare(sent, _expected_sign(params, with_prepare_id=with_prepare_id))


def _lookup_tenant(merchant_trans_id):
    try:
        return Tenant.objects.get(pk=int(merchant_trans_id))
    except (Tenant.DoesNotExist, ValueError, TypeError):
        return None


def handle_prepare(params: dict) -> dict:
    """Validate the Prepare callback. No money moves here — just confirm we
    recognise the tenant and the amount is sane."""
    base = {
        'click_trans_id': params.get('click_trans_id'),
        'merchant_trans_id': params.get('merchant_trans_id'),
    }
    if not settings.CLICK_SECRET_KEY:
        return {**base, 'error': ERR_SIGN_CHECK_FAILED, 'error_note': 'Not configured'}
    if not _signature_ok(params, with_prepare_id=False):
        return {**base, 'error': ERR_SIGN_CHECK_FAILED, 'error_note': 'SIGN CHECK FAILED'}

    tenant = _lookup_tenant(params.get('merchant_trans_id'))
    if tenant is None:
        return {**base, 'error': ERR_USER_NOT_FOUND, 'error_note': 'User not found'}

    try:
        amount = Decimal(str(params.get('amount'))).quantize(Decimal('0.01'))
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return {**base, 'error': ERR_INCORRECT_AMOUNT, 'error_note': 'Incorrect amount'}

    # Reserve the order: persist the prepared amount keyed by click_trans_id so
    # Complete can refuse a mismatched amount. Idempotent if Click re-sends.
    ClickTransaction.objects.update_or_create(
        click_trans_id=str(params.get('click_trans_id') or ''),
        defaults={'tenant': tenant, 'amount': amount},
    )

    return {
        **base,
        'merchant_prepare_id': params.get('click_trans_id'),
        'error': ERR_OK,
        'error_note': 'Success',
    }


def handle_complete(params: dict) -> dict:
    """Validate the Complete callback and credit the wallet."""
    base = {
        'click_trans_id': params.get('click_trans_id'),
        'merchant_trans_id': params.get('merchant_trans_id'),
    }
    if not settings.CLICK_SECRET_KEY:
        return {**base, 'error': ERR_SIGN_CHECK_FAILED, 'error_note': 'Not configured'}
    if not _signature_ok(params, with_prepare_id=True):
        return {**base, 'error': ERR_SIGN_CHECK_FAILED, 'error_note': 'SIGN CHECK FAILED'}

    # Fail CLOSED on the payment result: only a present, non-negative `error`
    # (0 = success per the Click spec) finalizes the payment. Missing / garbled
    # / negative → treat as cancelled and credit nothing.
    raw_error = params.get('error')
    try:
        click_error = int(raw_error)
    except (ValueError, TypeError):
        click_error = -1  # absent / non-numeric → not a success
    if click_error != 0:
        return {**base, 'error': ERR_TRANSACTION_CANCELLED, 'error_note': 'Transaction cancelled'}

    # Match against the amount reserved on Prepare — never credit an amount we
    # didn't prepare for this click_trans_id.
    click_trans_id = str(params.get('click_trans_id') or '')
    prepared = ClickTransaction.objects.filter(click_trans_id=click_trans_id).first()
    if prepared is None:
        return {**base, 'error': ERR_USER_NOT_FOUND, 'error_note': 'Transaction not prepared'}

    try:
        amount = Decimal(str(params.get('amount'))).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return {**base, 'error': ERR_INCORRECT_AMOUNT, 'error_note': 'Incorrect amount'}
    if amount != prepared.amount:
        return {**base, 'error': ERR_INCORRECT_AMOUNT, 'error_note': 'Amount mismatch'}

    payment = credit_balance(
        prepared.tenant, amount,
        source=Payment.Source.CLICK,
        external_id=click_trans_id,
        note='Click.uz top-up',
    )
    if not prepared.completed:
        prepared.completed = True
        prepared.completed_at = timezone.now()
        prepared.save(update_fields=['completed', 'completed_at'])

    return {
        **base,
        'merchant_confirm_id': payment.pk,
        'error': ERR_OK,
        'error_note': 'Success',
    }
