"""Tests for the /api/v1/register endpoint and the LicenseKey lookup
helper. Heartbeat tests come with the heartbeat endpoint commit."""
import json

import pytest
from django.test import Client


pytestmark = pytest.mark.django_db


def _client():
    return Client()


def _register(payload):
    return _client().post(
        '/api/v1/register',
        data=json.dumps(payload),
        content_type='application/json',
    )


class TestRegisterHappyPath:
    def test_unbound_invite_creates_tenant_and_returns_key(self, invite_code):
        resp = _register({
            'email': 'owner@plov.uz',
            'org_name': 'Plov Plus',
            'invite_code': invite_code.code,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body['success'] is True
        assert len(body['key']) >= 32  # 48 bytes urlsafe is ~64 chars
        assert body['tenant_id']
        assert body['expires_at'] is None

        # Server stored sha256(key) and prefix only.
        from licenses.models import LicenseKey, _hash_key
        row = LicenseKey.objects.get(tenant_id=body['tenant_id'])
        assert row.key_hash == _hash_key(body['key'])
        assert row.key_prefix == body['key'][:8]

        # Invite is now consumed and bound to the tenant.
        invite_code.refresh_from_db()
        assert invite_code.consumed_at is not None
        assert invite_code.tenant_id == body['tenant_id']

    def test_bound_invite_matching_payload_succeeds(self, bound_invite_code):
        resp = _register({
            'email': 'plov@example.com',
            'org_name': 'plov plus',  # different case — should still match
            'invite_code': bound_invite_code.code,
        })
        assert resp.status_code == 201


class TestRegisterRejections:
    def test_missing_fields_returns_422(self):
        resp = _register({'email': 'x@y.local'})
        assert resp.status_code == 422
        body = resp.json()
        assert body['success'] is False
        assert set(body['errors']) == {'org_name', 'invite_code'}

    def test_invalid_json_returns_400(self):
        resp = _client().post(
            '/api/v1/register', data='not json',
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_unknown_invite_returns_404(self):
        resp = _register({
            'email': 'x@y.local', 'org_name': 'Z', 'invite_code': 'nope',
        })
        assert resp.status_code == 404

    def test_already_consumed_returns_409(self, invite_code):
        _register({
            'email': 'a@b.local', 'org_name': 'Café A',
            'invite_code': invite_code.code,
        })
        resp = _register({
            'email': 'c@d.local', 'org_name': 'Café C',
            'invite_code': invite_code.code,
        })
        assert resp.status_code == 409

    def test_expired_invite_returns_410(self, db):
        from datetime import timedelta
        from django.utils import timezone
        from tenants.models import InviteCode
        old = InviteCode.objects.create(
            expires_at=timezone.now() - timedelta(days=1),
        )
        resp = _register({
            'email': 'a@b.local', 'org_name': 'Z', 'invite_code': old.code,
        })
        assert resp.status_code == 410

    def test_bound_invite_wrong_email_returns_403(self, bound_invite_code):
        resp = _register({
            'email': 'other@example.com',
            'org_name': 'Plov Plus',
            'invite_code': bound_invite_code.code,
        })
        assert resp.status_code == 403
        assert 'email' in resp.json()['message'].lower()

    def test_bound_invite_wrong_org_returns_403(self, bound_invite_code):
        resp = _register({
            'email': 'plov@example.com',
            'org_name': 'Other Cafe',
            'invite_code': bound_invite_code.code,
        })
        assert resp.status_code == 403
        assert 'organization' in resp.json()['message'].lower()


class TestRegisterIdempotencyOnRetry:
    """If the customer's first /register succeeds but the response is
    lost (network blip), a retry with the SAME invite_code should fail
    with 409 — not double-burn it. Wraps the invite in select_for_update
    so concurrent POSTs serialize."""

    def test_double_register_returns_409_on_second(self, invite_code):
        first = _register({
            'email': 'a@b.local', 'org_name': 'A',
            'invite_code': invite_code.code,
        })
        assert first.status_code == 201

        second = _register({
            'email': 'a@b.local', 'org_name': 'A',
            'invite_code': invite_code.code,
        })
        assert second.status_code == 409


class TestKeyLookup:
    """Heartbeat handlers will call LicenseKey.lookup_by_cleartext on
    every request — make sure it does the constant-time compare."""

    def test_lookup_returns_row_for_valid_key(self, db):
        from licenses.models import LicenseKey
        from tenants.models import Tenant

        t = Tenant.objects.create(org_name='X', email='x@x.local')
        row, cleartext = LicenseKey.issue(t)
        looked_up = LicenseKey.lookup_by_cleartext(cleartext)
        assert looked_up is not None
        assert looked_up.pk == row.pk

    def test_lookup_returns_none_for_unknown(self, db):
        from licenses.models import LicenseKey
        assert LicenseKey.lookup_by_cleartext('totally-fake-key-1234567890') is None

    def test_lookup_returns_none_for_empty(self, db):
        from licenses.models import LicenseKey
        assert LicenseKey.lookup_by_cleartext('') is None
        assert LicenseKey.lookup_by_cleartext(None) is None
