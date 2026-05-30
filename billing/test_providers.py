"""Tests for the Click.uz and Payme.uz top-up webhooks.

No real provider is contacted — we build correctly-signed Click callbacks and
JSON-RPC Payme requests by hand and assert the wallet ends up credited exactly
once (idempotency matters: providers retry callbacks)."""
import base64
import hashlib
import json
from decimal import Decimal

import pytest
from django.test import Client

from billing.models import Payment, PaymeTransaction
from tenants.models import Tenant


pytestmark = pytest.mark.django_db

CLICK_SECRET = 'test-click-secret'
PAYME_KEY = 'test-payme-key'


def _tenant(balance=0):
    return Tenant.objects.create(org_name='Café', email='c@x.local', balance=balance)


# --- Click ----------------------------------------------------------------

def _click_sign(parts):
    return hashlib.md5(''.join(str(p) for p in parts).encode()).hexdigest()


def _click_prepare_payload(tenant_id, *, amount='50000', click_trans_id='CT-1'):
    service_id, sign_time = '123', '2026-05-28 12:00:00'
    sign = _click_sign([
        click_trans_id, service_id, CLICK_SECRET, tenant_id, amount, '0', sign_time,
    ])
    return {
        'click_trans_id': click_trans_id, 'service_id': service_id,
        'merchant_trans_id': str(tenant_id), 'amount': amount,
        'action': '0', 'sign_time': sign_time, 'sign_string': sign,
    }


def _click_complete_payload(tenant_id, *, amount='50000', click_trans_id='CT-1', error='0'):
    service_id, sign_time = '123', '2026-05-28 12:00:00'
    prepare_id = click_trans_id
    sign = _click_sign([
        click_trans_id, service_id, CLICK_SECRET, tenant_id,
        prepare_id, amount, '1', sign_time,
    ])
    payload = {
        'click_trans_id': click_trans_id, 'service_id': service_id,
        'merchant_trans_id': str(tenant_id), 'merchant_prepare_id': prepare_id,
        'amount': amount, 'action': '1',
        'sign_time': sign_time, 'sign_string': sign,
    }
    if error is not None:
        payload['error'] = error
    return payload


def _click(path, payload):
    return Client().post(f'/api/billing/click/{path}', data=payload)


class TestClick:
    def test_prepare_then_complete_credits_wallet(self, settings):
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        t = _tenant()
        assert _click('prepare', _click_prepare_payload(t.pk)).json()['error'] == 0
        body = _click('complete', _click_complete_payload(t.pk)).json()
        assert body['error'] == 0
        t.refresh_from_db()
        assert t.balance == Decimal('50000.00')
        assert Payment.objects.filter(
            tenant=t, source=Payment.Source.CLICK, external_id='CT-1',
        ).count() == 1

    def test_bad_signature_rejected(self, settings):
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        t = _tenant()
        payload = _click_complete_payload(t.pk)
        payload['sign_string'] = 'deadbeef'
        assert _click('complete', payload).json()['error'] == _err_sign()
        t.refresh_from_db()
        assert t.balance == Decimal('0.00')

    def test_complete_is_idempotent_on_retry(self, settings):
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        t = _tenant()
        _click('prepare', _click_prepare_payload(t.pk))
        _click('complete', _click_complete_payload(t.pk))
        _click('complete', _click_complete_payload(t.pk))  # retry
        t.refresh_from_db()
        assert t.balance == Decimal('50000.00')  # credited once
        assert Payment.objects.filter(source=Payment.Source.CLICK).count() == 1

    def test_complete_amount_mismatch_rejected(self, settings):
        """Vuln fix: a Complete must match the amount reserved on Prepare."""
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        t = _tenant()
        _click('prepare', _click_prepare_payload(t.pk, amount='50000'))
        # Validly-signed Complete for a DIFFERENT amount → refused, no credit.
        resp = _click('complete', _click_complete_payload(t.pk, amount='999999'))
        assert resp.json()['error'] == _err_amount()
        t.refresh_from_db()
        assert t.balance == Decimal('0.00')

    def test_complete_missing_error_fails_closed(self, settings):
        """Vuln fix: a Complete with no `error` field must NOT be credited."""
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        t = _tenant()
        _click('prepare', _click_prepare_payload(t.pk))
        resp = _click('complete', _click_complete_payload(t.pk, error=None))
        assert resp.json()['error'] != 0  # not a success
        t.refresh_from_db()
        assert t.balance == Decimal('0.00')

    def test_prepare_unknown_tenant(self, settings):
        settings.CLICK_SECRET_KEY = CLICK_SECRET
        assert _click('prepare', _click_prepare_payload(999999)).json()['error'] == -5


def _err_sign():
    from billing.services.click import ERR_SIGN_CHECK_FAILED
    return ERR_SIGN_CHECK_FAILED


def _err_amount():
    from billing.services.click import ERR_INCORRECT_AMOUNT
    return ERR_INCORRECT_AMOUNT


# --- Payme ----------------------------------------------------------------

def _payme_headers(key=PAYME_KEY):
    token = base64.b64encode(f'Paycom:{key}'.encode()).decode()
    return {'HTTP_AUTHORIZATION': f'Basic {token}'}


def _payme_call(method, params, *, key=PAYME_KEY):
    return Client().post(
        '/api/billing/payme',
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}),
        content_type='application/json',
        **_payme_headers(key),
    )


class TestPayme:
    def test_auth_required(self, settings):
        settings.PAYME_MERCHANT_KEY = PAYME_KEY
        resp = _payme_call('CheckPerformTransaction',
                           {'amount': 5000000, 'account': {'tenant_id': 1}},
                           key='wrong-key')
        assert resp.json()['error']['code'] == -32504

    def test_full_create_perform_flow_credits_once(self, settings):
        settings.PAYME_MERCHANT_KEY = PAYME_KEY
        t = _tenant()
        acct = {'amount': 5000000, 'account': {'tenant_id': t.pk}}  # 50000.00 so'm

        assert _payme_call('CheckPerformTransaction', acct).json()['result']['allow'] is True

        created = _payme_call('CreateTransaction',
                              {'id': 'PT-1', 'time': 1, **acct}).json()
        assert created['result']['state'] == 1

        performed = _payme_call('PerformTransaction', {'id': 'PT-1'}).json()
        assert performed['result']['state'] == 2

        t.refresh_from_db()
        assert t.balance == Decimal('50000.00')

        # Perform retry is idempotent — no double credit.
        _payme_call('PerformTransaction', {'id': 'PT-1'})
        t.refresh_from_db()
        assert t.balance == Decimal('50000.00')
        assert Payment.objects.filter(source=Payment.Source.PAYME).count() == 1

    def test_unknown_account_rejected(self, settings):
        settings.PAYME_MERCHANT_KEY = PAYME_KEY
        resp = _payme_call('CheckPerformTransaction',
                           {'amount': 1000, 'account': {'tenant_id': 999999}})
        assert resp.json()['error']['code'] == -31050

    def test_cancel_after_perform_marks_state_minus_two(self, settings):
        settings.PAYME_MERCHANT_KEY = PAYME_KEY
        t = _tenant()
        acct = {'amount': 100000, 'account': {'tenant_id': t.pk}}
        _payme_call('CreateTransaction', {'id': 'PT-2', 'time': 1, **acct})
        _payme_call('PerformTransaction', {'id': 'PT-2'})
        cancelled = _payme_call('CancelTransaction', {'id': 'PT-2', 'reason': 5}).json()
        assert cancelled['result']['state'] == -2
        assert PaymeTransaction.objects.get(payme_id='PT-2').state == -2
