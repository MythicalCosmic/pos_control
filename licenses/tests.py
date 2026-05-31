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
    def test_email_only_redeems_pre_bound_invite(self, bound_invite_code):
        """The wizard sends only the email; the control center finds the
        invite the vendor pre-issued for that address — "email verified by
        vendor"."""
        resp = _register({'email': 'plov@example.com'})
        assert resp.status_code == 201
        body = resp.json()
        assert body['success'] is True
        assert len(body['key']) >= 32

        # Server stored sha256(key) and prefix only.
        from licenses.models import LicenseKey, _hash_key
        row = LicenseKey.objects.get(tenant_id=body['tenant_id'])
        assert row.key_hash == _hash_key(body['key'])

        # Invite consumed and bound to the resulting tenant.
        bound_invite_code.refresh_from_db()
        assert bound_invite_code.consumed_at is not None
        assert bound_invite_code.tenant_id == body['tenant_id']

        # Tenant org_name pulled from the invite's intended_org_name.
        from tenants.models import Tenant
        tenant = Tenant.objects.get(pk=body['tenant_id'])
        assert tenant.org_name == 'Plov Plus'

    def test_email_case_is_folded_on_lookup(self, bound_invite_code):
        resp = _register({'email': 'PLOV@example.com'})
        assert resp.status_code == 201

    def test_explicit_invite_code_path_still_works(self, invite_code):
        """Legacy: vendor mails the customer a printed code — they type it
        in alongside their email. Unbound invites accept any email."""
        resp = _register({
            'email': 'owner@plov.uz',
            'org_name': 'Plov Plus',
            'invite_code': invite_code.code,
        })
        assert resp.status_code == 201
        body = resp.json()
        invite_code.refresh_from_db()
        assert invite_code.consumed_at is not None
        assert invite_code.tenant_id == body['tenant_id']


class TestRegisterRejections:
    def test_missing_email_returns_422(self):
        resp = _register({'org_name': 'x'})
        assert resp.status_code == 422
        body = resp.json()
        assert body['success'] is False
        assert 'email' in body['errors']

    def test_invalid_json_returns_400(self):
        resp = _client().post(
            '/api/v1/register', data='not json',
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_no_pre_bound_invite_returns_403(self):
        """No invite_code, no pre-bound invite for this email → vendor
        hasn't verified this address. 403."""
        resp = _register({'email': 'stranger@example.com'})
        assert resp.status_code == 403
        assert 'no pending invite' in resp.json()['message'].lower()

    def test_unknown_invite_code_returns_404(self):
        resp = _register({
            'email': 'x@y.local', 'org_name': 'Z', 'invite_code': 'nope',
        })
        assert resp.status_code == 404

    def test_already_consumed_invite_code_returns_409(self, invite_code):
        _register({
            'email': 'a@b.local', 'org_name': 'Café A',
            'invite_code': invite_code.code,
        })
        resp = _register({
            'email': 'c@d.local', 'org_name': 'Café C',
            'invite_code': invite_code.code,
        })
        assert resp.status_code == 409

    def test_expired_invite_code_returns_410(self, db):
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

    def test_bound_invite_code_wrong_email_returns_403(self, bound_invite_code):
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

    def test_expired_pre_bound_invite_falls_through_to_403(self, db):
        """An expired pre-bound invite is invisible to the email-only
        lookup — same as if no invite existed at all."""
        from datetime import timedelta
        from django.utils import timezone
        from tenants.models import InviteCode
        InviteCode.objects.create(
            intended_email='ghost@example.com',
            expires_at=timezone.now() - timedelta(days=1),
        )
        resp = _register({'email': 'ghost@example.com'})
        assert resp.status_code == 403


class TestRegisterIdempotencyOnRetry:
    """A retried /register (network blip) must not double-burn the invite."""

    def test_double_register_email_only_returns_403_on_second(self, bound_invite_code):
        first = _register({'email': 'plov@example.com'})
        assert first.status_code == 201
        # Invite is now consumed; second call finds none unconsumed → 403.
        second = _register({'email': 'plov@example.com'})
        assert second.status_code == 403

    def test_double_register_with_code_returns_409_on_second(self, invite_code):
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
        from billing.models import Payment, Subscription
        from billing.services.billing import credit_balance
        row, key = self._issue()
        # A priced plan with a funded wallet → settle charges one period and
        # the heartbeat reports ACTIVE with a paid-through date + balance.
        Subscription.objects.create(tenant=row.tenant, price=10, period_days=30)
        credit_balance(row.tenant, 100, source=Payment.Source.MANUAL)

        resp = _heartbeat(key, {'client_version': 'alpha_pos@dev'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['status'] == 'ACTIVE'
        assert body['expires_at']  # paid_through
        assert body['server_now']
        assert body['next_heartbeat_in_s'] == 300
        assert body['ack_id']
        # Prepaid-billing fields: 100 topped up minus one 10 charge = 90.
        assert body['balance'] == '90.00'
        assert body['days_remaining'] is not None
        assert body['warn'] is False  # ~30 days left, well outside warn window

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

    def test_suspended_tenant_is_not_charged(self):
        """A manually-suspended tenant is paused, not billed — even when a
        subscription charge is due."""
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from billing.models import Payment, Subscription
        from billing.services.billing import credit_balance
        from licenses.models import LicenseKey
        row, key = self._issue()
        Subscription.objects.create(tenant=row.tenant, price=10, period_days=30)
        credit_balance(row.tenant, 100, source=Payment.Source.MANUAL)  # → balance 90
        # Make a charge due, then suspend the key.
        Subscription.objects.filter(tenant=row.tenant).update(
            paid_through=timezone.now() - timedelta(seconds=1),
        )
        row.status = LicenseKey.Status.SUSPENDED
        row.save()

        resp = _heartbeat(key)
        assert resp.json()['status'] == 'SUSPENDED'
        row.tenant.refresh_from_db()
        # Still 90 — NOT charged the extra 10 while suspended.
        assert row.tenant.balance == Decimal('90.00')

    def test_unpaid_balance_returns_expired_without_mutating_row(self):
        from billing.models import Subscription
        from licenses.models import LicenseKey
        row, key = self._issue()
        # Priced plan, empty wallet → can't fund the period → EXPIRED.
        Subscription.objects.create(tenant=row.tenant, price=10, period_days=30)

        resp = _heartbeat(key)
        assert resp.status_code == 200
        assert resp.json()['status'] == 'EXPIRED'

        # Key row stays ACTIVE — the EXPIRED verdict is money-derived each
        # heartbeat, not a flag on the key. The vendor revives by topping up,
        # not by flipping status.
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

    def test_garbage_xforwarded_for_does_not_crash(self, db):
        """A forged/garbled X-Forwarded-For must not reach the inet column
        (would DataError → 500 on Postgres). The junk is dropped — we fall
        back to a validated REMOTE_ADDR (or NULL) — and the heartbeat still
        succeeds with the event recorded."""
        from licenses.models import LicenseKey, HeartbeatEvent
        from tenants.models import Tenant
        t = Tenant.objects.create(org_name='X', email='x@x.local')
        row, key = LicenseKey.issue(t)
        resp = _client().post(
            '/api/v1/heartbeat', data='{}', content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
            HTTP_X_FORWARDED_FOR='not-an-ip-address',
        )
        assert resp.status_code == 200
        evt = HeartbeatEvent.objects.get(license_key=row)
        # The garbage header is never stored; ip is NULL or a valid fallback.
        assert evt.ip != 'not-an-ip-address'
        if evt.ip is not None:
            import ipaddress
            ipaddress.ip_address(evt.ip)  # raises if somehow invalid

    def test_valid_xforwarded_for_is_recorded(self, db):
        from licenses.models import LicenseKey, HeartbeatEvent
        from tenants.models import Tenant
        t = Tenant.objects.create(org_name='X', email='x@x.local')
        row, key = LicenseKey.issue(t)
        resp = _client().post(
            '/api/v1/heartbeat', data='{}', content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
            HTTP_X_FORWARDED_FOR='203.0.113.7, 10.0.0.1',
        )
        assert resp.status_code == 200
        evt = HeartbeatEvent.objects.get(license_key=row)
        assert evt.ip == '203.0.113.7'


# ---------------------------------------------------------------------------
# Admin bulk actions + ControlEvent audit trail
# ---------------------------------------------------------------------------


def _staff_user(db):
    from django.contrib.auth.models import User
    return User.objects.create_user(
        username='staff', password='pw', is_staff=True, is_superuser=True,
    )


def _staff_client(user):
    """Django test client logged in as `user`. Uses force_login because
    we don't care about exercising the auth form — just admin actions."""
    c = Client()
    c.force_login(user)
    return c


def _issue_active_key():
    from licenses.models import LicenseKey
    from tenants.models import Tenant
    t = Tenant.objects.create(org_name='Café X', email='x@x.local')
    row, _key = LicenseKey.issue(t)
    return row


class TestAdminActions:
    def test_suspend_action_flips_status_and_writes_audit(self, db):
        from licenses.models import LicenseKey, ControlEvent
        user = _staff_user(db)
        row = _issue_active_key()

        resp = _staff_client(user).post('/admin/licenses/licensekey/', {
            'action': 'suspend_selected',
            '_selected_action': [str(row.pk)],
        })
        # Admin actions redirect back to the changelist on success.
        assert resp.status_code in (200, 302)

        row.refresh_from_db()
        assert row.status == LicenseKey.Status.SUSPENDED

        events = ControlEvent.objects.filter(license_key=row,
                                             action=ControlEvent.Action.SUSPEND)
        assert events.count() == 1
        assert events.first().actor == user

    def test_resume_action(self, db):
        from licenses.models import LicenseKey, ControlEvent
        user = _staff_user(db)
        row = _issue_active_key()
        row.status = LicenseKey.Status.SUSPENDED
        row.save()

        _staff_client(user).post('/admin/licenses/licensekey/', {
            'action': 'resume_selected',
            '_selected_action': [str(row.pk)],
        })
        row.refresh_from_db()
        assert row.status == LicenseKey.Status.ACTIVE
        assert ControlEvent.objects.filter(
            license_key=row, action=ControlEvent.Action.RESUME,
        ).count() == 1

    def test_clear_message_action(self, db):
        from licenses.models import ControlEvent
        user = _staff_user(db)
        row = _issue_active_key()
        row.message = 'Subscription renews soon'
        row.save()

        _staff_client(user).post('/admin/licenses/licensekey/', {
            'action': 'clear_message',
            '_selected_action': [str(row.pk)],
        })
        row.refresh_from_db()
        assert row.message == ''
        assert ControlEvent.objects.filter(
            license_key=row, action=ControlEvent.Action.SET_MESSAGE,
        ).exists()


class TestSaveModelAudit:
    """Edits via the admin change form (not the bulk actions) should
    also write ControlEvents."""

    def test_status_flip_via_admin_form_writes_event(self, db):
        from licenses.models import LicenseKey, ControlEvent
        from django.contrib.auth.models import User
        from licenses.admin import LicenseKeyAdmin
        from django.contrib.admin.sites import AdminSite

        user = User.objects.create_user(username='s', password='p', is_staff=True)
        row = _issue_active_key()
        before = LicenseKey.objects.get(pk=row.pk)

        # Simulate the admin's save flow: status changed in memory, then
        # save_model called by Django's admin internals.
        row.status = LicenseKey.Status.SUSPENDED

        class _FakeRequest:
            user = None
        req = _FakeRequest(); req.user = user

        admin = LicenseKeyAdmin(LicenseKey, AdminSite())
        admin.save_model(req, row, form=None, change=True)

        assert ControlEvent.objects.filter(
            license_key=row, action=ControlEvent.Action.SUSPEND,
        ).exists()

    def test_revoke_via_admin_form_stamps_revoked_at(self, db):
        from licenses.models import LicenseKey
        from django.contrib.auth.models import User
        from licenses.admin import LicenseKeyAdmin
        from django.contrib.admin.sites import AdminSite

        user = User.objects.create_user(username='s', password='p', is_staff=True)
        row = _issue_active_key()
        assert row.revoked_at is None

        row.status = LicenseKey.Status.REVOKED

        class _FakeRequest:
            user = None
        req = _FakeRequest(); req.user = user

        admin = LicenseKeyAdmin(LicenseKey, AdminSite())
        admin.save_model(req, row, form=None, change=True)

        row.refresh_from_db()
        assert row.status == LicenseKey.Status.REVOKED
        assert row.revoked_at is not None


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------


class TestGenerateVendorKeypair:
    def test_prints_both_halves(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('generate_vendor_keypair', stdout=out)
        text = out.getvalue()
        # Two 64-char hex strings (32 bytes each) must appear in the output.
        import re
        hexes = re.findall(r'\b[0-9a-f]{64}\b', text)
        assert len(hexes) >= 2
        priv_hex, pub_hex = hexes[0], hexes[1]

        # The pubkey must match what you'd derive from the priv — proves
        # the printed pair is internally consistent.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
        derived_pub_hex = priv.public_key().public_bytes_raw().hex()
        assert derived_pub_hex == pub_hex


class TestGenerateUnlock:
    def test_signs_with_configured_private_key(self, settings):
        import base64
        import json as _json
        from io import StringIO
        from django.core.management import call_command
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        # Generate a real keypair so we can verify the signature ourselves.
        priv = Ed25519PrivateKey.generate()
        settings.LICENSE_VENDOR_PRIVATE_KEY = priv.private_bytes(
            encoding=__import__('cryptography').hazmat.primitives.serialization.Encoding.Raw,
            format=__import__('cryptography').hazmat.primitives.serialization.PrivateFormat.Raw,
            encryption_algorithm=__import__('cryptography').hazmat.primitives.serialization.NoEncryption(),
        ).hex()

        out = StringIO()
        call_command(
            'generate_unlock',
            signed_at='2099-01-01T00:00:00+00:00',
            stdout=out,
        )
        text = out.getvalue()
        # The base64 blob is the last non-empty line that isn't part of
        # the helper text — grab anything that decodes to JSON.
        candidate = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                decoded = base64.b64decode(line, validate=False)
                blob = _json.loads(decoded)
                if isinstance(blob, dict) and 'signature' in blob:
                    candidate = blob
                    break
            except Exception:
                continue
        assert candidate is not None
        assert candidate['payload'] == 'PERPETUAL_UNLOCK_v1'
        assert candidate['signed_at'] == '2099-01-01T00:00:00+00:00'

        # Round-trip: verify the signature matches what the alpha_pos
        # client would expect.
        sig = bytes.fromhex(candidate['signature'])
        msg = f"PERPETUAL_UNLOCK_v1|{candidate['signed_at']}".encode('utf-8')
        priv.public_key().verify(sig, msg)  # raises on failure

    def test_missing_private_key_raises(self, settings):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        settings.LICENSE_VENDOR_PRIVATE_KEY = ''
        with pytest.raises(CommandError):
            call_command('generate_unlock')

    def test_invalid_hex_private_key_raises(self, settings):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        settings.LICENSE_VENDOR_PRIVATE_KEY = 'not-hex-zzzz'
        with pytest.raises(CommandError):
            call_command('generate_unlock')
