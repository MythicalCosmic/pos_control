"""Payme.uz (Paycom) Merchant API → top up a tenant's wallet.

Payme posts JSON-RPC 2.0 to a single endpoint, authenticated with HTTP Basic
(password = ``PAYME_MERCHANT_KEY``). The stateful method set:

- ``CheckPerformTransaction`` — may this account be charged this amount?
- ``CreateTransaction`` — register a pending transaction.
- ``PerformTransaction`` — money confirmed → credit the wallet.
- ``CancelTransaction`` — roll a transaction back.
- ``CheckTransaction`` — report a transaction's lifecycle.

The ``account`` object carries ``{"tenant_id": <pk>}`` — the reference the
vendor embeds in the customer's Payme checkout. Amounts are in **tiyin**
(UZS×100); we store and credit so'm.

Docs: https://developer.help.paycom.uz/en/protokol-merchant-api/
"""
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from billing.models import Payment, PaymeTransaction
from billing.services.billing import credit_balance, debit_balance
from tenants.models import Tenant


logger = logging.getLogger(__name__)


# JSON-RPC / Payme error codes.
ERR_INSUFFICIENT_PRIVILEGE = -32504
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_UNABLE_TO_PERFORM = -31008
ERR_ACCOUNT_NOT_FOUND = -31050  # `data` names the offending account field


def _now_ms() -> int:
    return int(time.time() * 1000)


class PaymeError(Exception):
    def __init__(self, code, message='', data=None):
        self.code = code
        self.message = message or 'error'
        self.data = data
        super().__init__(self.message)


def _err_obj(code, message, data=None):
    # Payme expects localized message dicts; keep it simple but well-formed.
    body = {'code': code, 'message': {'ru': message, 'en': message, 'uz': message}}
    if data is not None:
        body['data'] = data
    return body


def check_auth(auth_header: str) -> bool:
    """HTTP Basic, password == PAYME_MERCHANT_KEY (login is ignored by spec)."""
    import base64
    key = settings.PAYME_MERCHANT_KEY
    if not key or not auth_header.lower().startswith('basic '):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip()).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return False
    _login, _, password = decoded.partition(':')
    from django.utils.crypto import constant_time_compare
    return constant_time_compare(password, key)


def _tenant_from_account(params):
    account = params.get('account') or {}
    raw = account.get('tenant_id')
    try:
        return Tenant.objects.get(pk=int(raw))
    except (Tenant.DoesNotExist, ValueError, TypeError):
        raise PaymeError(ERR_ACCOUNT_NOT_FOUND, 'Tenant not found', data='tenant_id')


def _somonu(amount_tiyin) -> Decimal:
    try:
        return (Decimal(str(amount_tiyin)) / Decimal('100')).quantize(Decimal('0.01'))
    except Exception:
        raise PaymeError(ERR_INVALID_AMOUNT, 'Incorrect amount')


def _check_perform(params):
    _tenant_from_account(params)
    if _somonu(params.get('amount')) <= 0:
        raise PaymeError(ERR_INVALID_AMOUNT, 'Incorrect amount')
    return {'allow': True}


def _create(params):
    payme_id = params.get('id')
    amount = _somonu(params.get('amount'))
    if amount <= 0:
        raise PaymeError(ERR_INVALID_AMOUNT, 'Incorrect amount')

    existing = PaymeTransaction.objects.filter(payme_id=payme_id).first()
    if existing is not None:
        if existing.state != PaymeTransaction.State.CREATED:
            raise PaymeError(ERR_UNABLE_TO_PERFORM, 'Transaction not creatable')
        return {
            'create_time': existing.create_time,
            'transaction': str(existing.pk),
            'state': existing.state,
        }

    tenant = _tenant_from_account(params)
    now = _now_ms()
    txn = PaymeTransaction.objects.create(
        payme_id=payme_id, tenant=tenant, amount=amount,
        state=PaymeTransaction.State.CREATED, create_time=now,
    )
    return {'create_time': now, 'transaction': str(txn.pk), 'state': txn.state}


def _perform(params):
    payme_id = params.get('id')
    with transaction.atomic():
        txn = PaymeTransaction.objects.select_for_update().filter(payme_id=payme_id).first()
        if txn is None:
            raise PaymeError(ERR_TRANSACTION_NOT_FOUND, 'Transaction not found')
        if txn.state not in (PaymeTransaction.State.CREATED,
                             PaymeTransaction.State.COMPLETED):
            raise PaymeError(ERR_UNABLE_TO_PERFORM, 'Transaction not performable')
        if txn.state == PaymeTransaction.State.CREATED:
            txn.state = PaymeTransaction.State.COMPLETED
            txn.perform_time = _now_ms()
            txn.save(update_fields=['state', 'perform_time'])

    # Credit on EVERY perform — including a retry of an already-COMPLETED txn.
    # credit_balance is idempotent on payme_id, so if the process crashed
    # between marking COMPLETED above and this line on a previous attempt, the
    # retry recovers it: the wallet is credited exactly once, never lost.
    credit_balance(
        txn.tenant, txn.amount,
        source=Payment.Source.PAYME, external_id=txn.payme_id,
        note='Payme.uz top-up',
    )
    return {'transaction': str(txn.pk), 'perform_time': txn.perform_time, 'state': txn.state}


def _cancel(params):
    payme_id = params.get('id')
    with transaction.atomic():
        txn = PaymeTransaction.objects.select_for_update().filter(payme_id=payme_id).first()
        if txn is None:
            raise PaymeError(ERR_TRANSACTION_NOT_FOUND, 'Transaction not found')
        if txn.state in (PaymeTransaction.State.CANCELLED,
                         PaymeTransaction.State.CANCELLED_AFTER_COMPLETE):
            # Already cancelled — idempotent, and the reversal (if any) already
            # ran on the first cancel. Don't debit again.
            return {'transaction': str(txn.pk), 'cancel_time': txn.cancel_time, 'state': txn.state}

        now = _now_ms()
        # COMPLETED means the wallet was credited on Perform → this cancel must
        # claw that money back. CREATED never credited, so nothing to reverse.
        was_completed = txn.state == PaymeTransaction.State.COMPLETED
        if was_completed:
            txn.state = PaymeTransaction.State.CANCELLED_AFTER_COMPLETE
        else:
            txn.state = PaymeTransaction.State.CANCELLED
        txn.cancel_time = now
        txn.reason = params.get('reason')
        txn.save(update_fields=['state', 'cancel_time', 'reason'])

    # Reverse the top-up credit outside the txn-row lock. Idempotent on the
    # ':reversal' external_id (distinct from the credit's id so it doesn't trip
    # the credit's unique row), so even a crash-retry can't double-debit. The
    # state guard above already prevents re-entry; this is belt-and-braces.
    if was_completed:
        debit_balance(
            txn.tenant, txn.amount,
            source=Payment.Source.PAYME,
            external_id=f'{txn.payme_id}:reversal',
            note='Payme.uz cancel-after-perform reversal',
        )
    return {'transaction': str(txn.pk), 'cancel_time': now, 'state': txn.state}


def _check(params):
    payme_id = params.get('id')
    txn = PaymeTransaction.objects.filter(payme_id=payme_id).first()
    if txn is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, 'Transaction not found')
    return {
        'create_time': txn.create_time,
        'perform_time': txn.perform_time,
        'cancel_time': txn.cancel_time,
        'transaction': str(txn.pk),
        'state': txn.state,
        'reason': txn.reason,
    }


_METHODS = {
    'CheckPerformTransaction': _check_perform,
    'CreateTransaction': _create,
    'PerformTransaction': _perform,
    'CancelTransaction': _cancel,
    'CheckTransaction': _check,
}


def dispatch(payload: dict) -> dict:
    """Route one JSON-RPC request to its handler, returning a JSON-RPC
    response dict (with ``result`` or ``error``). Auth is checked by the view
    before this is called."""
    rpc_id = payload.get('id')
    method = payload.get('method')
    params = payload.get('params') or {}

    handler = _METHODS.get(method)
    if handler is None:
        return {'jsonrpc': '2.0', 'id': rpc_id,
                'error': _err_obj(ERR_METHOD_NOT_FOUND, 'Method not found')}
    try:
        result = handler(params)
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}
    except PaymeError as exc:
        return {'jsonrpc': '2.0', 'id': rpc_id,
                'error': _err_obj(exc.code, exc.message, exc.data)}
    except Exception:
        logger.exception('payme: unhandled error in %s', method)
        return {'jsonrpc': '2.0', 'id': rpc_id,
                'error': _err_obj(ERR_UNABLE_TO_PERFORM, 'Internal error')}
