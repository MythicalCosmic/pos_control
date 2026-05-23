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


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def _heartbeat(key, body=None, *, header_override=None):
    """Helper: send a bearer-authed heartbeat with the given key + body."""
    headers = {'HTTP_AUTHORIZATION': f'Bearer {key}'}
    if header_override is not None:
        headers = header_override
    return _client().post(
        '/api/v1/heartbeat',
        data=json.dumps(body or {}),
        content_type='application/json',
        **headers,
    )


class TestHeartbeatAuth:
    def test_missing_bearer_returns_401(self, db):
        resp = _client().post(
            '/api/v1/heartbeat',
            data=json.dumps({}),
            content_type='application/json',
        )
        assert resp.status_code == 401

    def test_bad_scheme_returns_401(self, db):
        resp = _heartbeat('whatever', header_override={
            'HTTP_AUTHORIZATION': 'NotBearer xxx',
        })
        assert resp.status_code == 401

    def test_unknown_key_returns_401(self, db):
        resp = _heartbeat('totally-fake-key-xxxxxxxx')
        assert resp.status_code == 401


class TestHeartbeatStatusComputation:
    def _issue(self, **kwargs):
        from licenses.models import LicenseKey
        from tenants.models import Tenant
        t = Tenant.objects.create(
            org_name=kwargs.pop('org', 'Demo'),
            email=kwargs.pop('email', 'demo@x.local'),
        )
        return LicenseKey.issue(t, **kwargs)

    def test_active_returns_active_with_ack(self):
        from datetime import timedelta
        from django.utils import timezone
        row, key = self._issue(
            expires_at=timezone.now() + timedelta(days=30),
        )
        resp = _heartbeat(key, {'client_version': 'alpha_pos@dev'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['status'] == 'ACTIVE'
        assert body['expires_at']
        assert body['server_now']
        assert body['next_heartbeat_in_s'] == 300
        assert body['ack_id']

        # HeartbeatEvent recorded with the payload kept.
        from licenses.models import HeartbeatEvent
        evt = HeartbeatEvent.objects.get(license_key=row)
        assert evt.client_version == 'alpha_pos@dev'
        assert str(evt.ack_id) == body['ack_id']

    def test_suspended_returns_suspended(self):
        from licenses.models import LicenseKey
        row, key = self._issue()
        row.status = LicenseKey.Status.SUSPENDED
        row.save()
        resp = _heartbeat(key)
        assert resp.status_code == 200
        assert resp.json()['status'] == 'SUSPENDED'

    def test_revoked_returns_410(self):
        from licenses.models import LicenseKey
        row, key = self._issue()
        row.status = LicenseKey.Status.REVOKED
        row.save()
        resp = _heartbeat(key)
        assert resp.status_code == 410

    def test_past_expiry_returns_expired_without_mutating_row(self):
        from datetime import timedelta
        from django.utils import timezone
        from licenses.models import LicenseKey
        row, key = self._issue(
            expires_at=timezone.now() - timedelta(hours=1),
        )
        resp = _heartbeat(key)
        assert resp.status_code == 200
        assert resp.json()['status'] == 'EXPIRED'

        # Row should still be ACTIVE — expiry is computed each heartbeat
        # against server_now. The vendor renews by extending expires_at,
        # not by flipping status back.
        row.refresh_from_db()
        assert row.status == LicenseKey.Status.ACTIVE

    def test_message_passes_through(self):
        row, key = self._issue()
        row.message = 'Subscription expires in 3 days'
        row.save()
        resp = _heartbeat(key)
        assert resp.json()['message'] == 'Subscription expires in 3 days'

    def test_empty_message_returns_null(self):
        row, key = self._issue()
        resp = _heartbeat(key)
        assert resp.json()['message'] is None


class TestHeartbeatRecording:
    def test_bad_body_doesnt_crash(self, db):
        from licenses.models import LicenseKey, HeartbeatEvent
        from tenants.models import Tenant
        t = Tenant.objects.create(org_name='X', email='x@x.local')
        row, key = LicenseKey.issue(t)

        # Empty body should still record the event.
        resp = _client().post(
            '/api/v1/heartbeat', data='', content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )
        assert resp.status_code == 200
        assert HeartbeatEvent.objects.filter(license_key=row).count() == 1

    def test_non_dict_body_is_tolerated(self, db):
        from licenses.models import LicenseKey
        from tenants.models import Tenant
        t = Tenant.objects.create(org_name='X', email='x@x.local')
        row, key = LicenseKey.issue(t)
        resp = _heartbeat(key, body='this is a string')  # type: ignore[arg-type]
        # heartbeat() requires a dict; pass a list directly to exercise
        # the json.loads -> dict guard.
