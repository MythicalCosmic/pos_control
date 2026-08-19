import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, override_settings
from django.utils import timezone

from billing.models import Payment, PlanChangeRequest, Subscription, SubscriptionPlan
from licenses.models import ControlEvent, HeartbeatEvent, LicenseKey
from tenants.models import Tenant
from vendor_api.models import IdempotencyRecord, LoginAttempt, VendorSession


pytestmark = pytest.mark.django_db


def post(client, path, body=None, token=None, key=None, method="post"):
    headers = {}
    if token:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if key:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    return getattr(client, method)(
        path,
        data=json.dumps(body or {}),
        content_type="application/json",
        **headers,
    )


@pytest.fixture
def operator():
    return User.objects.create_superuser(
        username="operator",
        password="correct horse battery staple",
        email="ops@example.com",
        first_name="Operations",
    )


@pytest.fixture
def auth(operator):
    client = Client()
    response = post(
        client,
        "/api/admin/auth/login",
        {
            "username": operator.username,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 200
    return client, response.json()["data"]["access_token"], response.json()["data"]


@pytest.fixture
def tenant():
    return Tenant.objects.create(
        org_name="Navoiy Coffee",
        email="owner@example.com",
        balance=Decimal("850000.00"),
    )


@pytest.fixture
def plan():
    return SubscriptionPlan.objects.create(
        code="pro",
        name="Pro",
        price=Decimal("350000.00"),
        period_days=30,
        warn_days=3,
        grace_days=3,
        sort_order=10,
    )


class TestAuthentication:
    def test_login_me_refresh_logout(self, operator):
        client = Client()
        login = post(
            client,
            "/api/admin/auth/login",
            {
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200
        data = login.json()["data"]
        assert data["expires_in"] == 900
        assert data["user"]["permissions"]
        assert VendorSession.objects.count() == 1
        assert data["access_token"] not in VendorSession.objects.get().access_hash
        assert login["Cache-Control"] == "no-store"

        me = client.get(
            "/api/admin/auth/me", HTTP_AUTHORIZATION=f"Bearer {data['access_token']}"
        )
        assert me.status_code == 200
        refresh = post(
            client,
            "/api/admin/auth/refresh",
            {
                "refresh_token": data["refresh_token"],
            },
        )
        assert refresh.status_code == 200
        rotated = refresh.json()["data"]
        assert rotated["access_token"] != data["access_token"]
        assert (
            client.get(
                "/api/admin/auth/me",
                HTTP_AUTHORIZATION=f"Bearer {data['access_token']}",
            ).status_code
            == 401
        )

        logout = post(client, "/api/admin/auth/logout", token=rotated["access_token"])
        assert logout.status_code == 200
        assert (
            client.get(
                "/api/admin/auth/me",
                HTTP_AUTHORIZATION=f"Bearer {rotated['access_token']}",
            ).status_code
            == 401
        )

    @override_settings(VENDOR_LOGIN_MAX_ATTEMPTS=2)
    def test_login_is_rate_limited_and_audited(self, operator):
        client = Client()
        for _ in range(2):
            assert (
                post(
                    client,
                    "/api/admin/auth/login",
                    {
                        "username": "operator",
                        "password": "wrong",
                    },
                ).status_code
                == 401
            )
        response = post(
            client,
            "/api/admin/auth/login",
            {
                "username": "operator",
                "password": "wrong",
            },
        )
        assert response.status_code == 429
        assert LoginAttempt.objects.filter(success=False).count() == 3

    def test_non_staff_cannot_login(self):
        User.objects.create_user(username="customer", password="password-long-enough")
        response = post(
            Client(),
            "/api/admin/auth/login",
            {
                "username": "customer",
                "password": "password-long-enough",
            },
        )
        assert response.status_code == 401

    def test_every_data_route_requires_bearer(self):
        assert Client().get("/api/admin/overview").status_code == 401

    def test_staff_can_read_but_needs_explicit_mutation_permission(self):
        User.objects.create_user(
            username="reader", password="password-long", is_staff=True
        )
        client = Client()
        login = post(
            client,
            "/api/admin/auth/login",
            {
                "username": "reader",
                "password": "password-long",
            },
        )
        token = login.json()["data"]["access_token"]
        assert (
            client.get(
                "/api/admin/tenants", HTTP_AUTHORIZATION=f"Bearer {token}"
            ).status_code
            == 200
        )
        denied = post(
            client,
            "/api/admin/tenants",
            {
                "org_name": "No",
                "email": "no@example.com",
            },
            token,
        )
        assert denied.status_code == 403

    @override_settings(TRUST_FORWARDED_FOR=True)
    def test_login_audit_uses_trusted_proxy_client_ip(self, operator):
        response = post(
            Client(),
            "/api/admin/auth/login",
            {"username": "operator", "password": "wrong"},
            method="post",
        )
        # Baseline request has no proxy header and still records REMOTE_ADDR.
        assert response.status_code == 401
        client = Client()
        post(
            client,
            "/api/admin/auth/login",
            {"username": "operator", "password": "wrong"},
            method="post",
        )
        proxied = client.post(
            "/api/admin/auth/login",
            data=json.dumps({"username": "operator", "password": "wrong"}),
            content_type="application/json",
            HTTP_X_FORWARDED_FOR="198.51.100.9, 203.0.113.7",
        )
        assert proxied.status_code == 401
        assert LoginAttempt.objects.latest("created_at").ip == "203.0.113.7"


class TestTenantAndLicenseControls:
    def test_tenant_create_patch_and_idempotency(self, auth):
        client, token, _ = auth
        payload = {"org_name": "Samarqand Cafe", "email": "hello@sam.local"}
        first = post(
            client, "/api/admin/tenants", payload, token, key="tenant-create-1"
        )
        second = post(
            client, "/api/admin/tenants", payload, token, key="tenant-create-1"
        )
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert Tenant.objects.filter(email="hello@sam.local").count() == 1
        assert IdempotencyRecord.objects.count() == 1

        conflict = post(
            client,
            "/api/admin/tenants",
            {"org_name": "Different", "email": "different@sam.local"},
            token,
            key="tenant-create-1",
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"

        tenant_id = first.json()["data"]["id"]
        patched = post(
            client,
            f"/api/admin/tenants/{tenant_id}",
            {"notes": "VIP"},
            token,
            method="patch",
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["notes"] == "VIP"

    def test_tenant_kill_switch_never_resumes_revoked(self, auth, tenant):
        client, token, _ = auth
        active, _ = LicenseKey.issue(tenant)
        revoked, _ = LicenseKey.issue(tenant)
        revoked.status = LicenseKey.Status.REVOKED
        revoked.revoked_at = timezone.now()
        revoked.save()
        response = post(
            client,
            f"/api/admin/tenants/{tenant.pk}/suspend",
            {"reason": "maintenance"},
            token,
        )
        assert response.status_code == 200
        assert response.json()["data"]["affected_license_count"] == 1
        active.refresh_from_db()
        assert active.status == LicenseKey.Status.SUSPENDED
        assert ControlEvent.objects.filter(action="SUSPEND").count() == 1

        resumed = post(client, f"/api/admin/tenants/{tenant.pk}/resume", {}, token)
        assert resumed.json()["data"]["affected_license_count"] == 1
        revoked.refresh_from_db()
        assert revoked.status == LicenseKey.Status.REVOKED

    def test_tenant_detail_encodes_invite_filter_url(self, auth):
        client, token, _ = auth
        tenant = Tenant.objects.create(
            org_name="Branch Cafe", email="owner+branch@example.com"
        )
        response = client.get(
            f"/api/admin/tenants/{tenant.pk}",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert response.status_code == 200
        assert response.json()["data"]["invites"]["url"].endswith(
            "q=owner%2Bbranch%40example.com"
        )

    def test_revoke_requires_reason_and_is_permanent(self, auth, tenant):
        client, token, _ = auth
        key, _ = LicenseKey.issue(tenant)
        assert (
            post(client, f"/api/admin/licenses/{key.pk}/revoke", {}, token).status_code
            == 422
        )
        response = post(
            client,
            f"/api/admin/licenses/{key.pk}/revoke",
            {"reason": "device retired"},
            token,
        )
        assert response.status_code == 200
        key.refresh_from_db()
        assert key.revoked_at is not None
        assert (
            post(client, f"/api/admin/licenses/{key.pk}/resume", {}, token).status_code
            == 409
        )
        key.status = LicenseKey.Status.ACTIVE
        with pytest.raises(ValidationError):
            key.save()

    def test_model_revoke_sets_timestamp_automatically(self, tenant):
        key, _ = LicenseKey.issue(tenant)
        key.status = LicenseKey.Status.REVOKED
        key.save(update_fields=["status"])
        key.refresh_from_db()
        assert key.revoked_at is not None

    def test_message_and_bulk_control(self, auth, tenant):
        client, token, _ = auth
        one, _ = LicenseKey.issue(tenant)
        two, _ = LicenseKey.issue(tenant)
        message = post(
            client,
            f"/api/admin/licenses/{one.pk}/message",
            {"message": "Maintenance Friday"},
            token,
        )
        assert message.status_code == 200
        bulk = post(
            client,
            "/api/admin/licenses/bulk-action",
            {
                "ids": [one.pk, two.pk],
                "action": "SUSPEND",
                "reason": "Scheduled branch maintenance",
            },
            token,
        )
        assert bulk.status_code == 200
        assert bulk.json()["data"]["affected_license_count"] == 2
        assert ControlEvent.objects.filter(
            action="SUSPEND",
            metadata__scope="bulk",
            metadata__reason="Scheduled branch maintenance",
        ).count() == 2
        cleared = post(
            client, f"/api/admin/licenses/{one.pk}/message", {}, token, method="delete"
        )
        assert cleared.status_code == 200

        invalid = post(
            client,
            "/api/admin/licenses/bulk-action",
            {"ids": [one.pk, "not-an-id"], "action": "RESUME"},
            token,
        )
        assert invalid.status_code == 422


class TestInvites:
    def test_create_detail_revoke_and_registration_rejection(self, auth):
        client, token, _ = auth
        created = post(
            client,
            "/api/admin/invites",
            {
                "intended_email": "new@example.com",
                "intended_org_name": "New Cafe",
                "expires_at": (timezone.now() + timedelta(days=7)).isoformat(),
            },
            token,
        )
        assert created.status_code == 201
        body = created.json()["data"]
        assert body["code"]
        assert ControlEvent.objects.filter(action="INVITE_CREATE").exists()
        invite_id = body["id"]
        detail = client.get(
            f"/api/admin/invites/{invite_id}", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert detail.json()["data"]["code"] == body["code"]
        revoked = post(
            client, f"/api/admin/invites/{invite_id}", {}, token, method="delete"
        )
        assert revoked.status_code == 200
        register = post(
            Client(),
            "/api/v1/register",
            {
                "email": "new@example.com",
                "invite_code": body["code"],
            },
        )
        assert register.status_code == 410

    @pytest.mark.parametrize("bad_expiry", [False, 0, 123, ["2026-09-01"]])
    def test_create_rejects_non_string_expiry(self, auth, bad_expiry):
        client, token, _ = auth
        response = post(
            client,
            "/api/admin/invites",
            {
                "intended_email": "new@example.com",
                "expires_at": bad_expiry,
            },
            token,
        )
        assert response.status_code == 422
        assert response.json()["errors"]["expires_at"]


class TestPlansSubscriptionsAndBilling:
    def test_plan_crud_retires_instead_of_deleting(self, auth):
        client, token, _ = auth
        response = post(
            client,
            "/api/admin/plans",
            {
                "code": "basic",
                "name": "Basic",
                "price": "100000.00",
                "period_days": 30,
                "warn_days": 3,
                "grace_days": 2,
            },
            token,
        )
        assert response.status_code == 201
        plan_id = response.json()["data"]["id"]
        patched = post(
            client,
            f"/api/admin/plans/{plan_id}",
            {"sort_order": 5},
            token,
            method="patch",
        )
        assert patched.status_code == 200
        retired = post(
            client, f"/api/admin/plans/{plan_id}", {}, token, method="delete"
        )
        assert retired.status_code == 200
        assert SubscriptionPlan.objects.get(pk=plan_id).is_active is False

    @pytest.mark.parametrize("bad_price", ["NaN", "Infinity", "1000000000000.00"])
    def test_plan_rejects_unsafe_decimal_values(self, auth, bad_price):
        client, token, _ = auth
        response = post(
            client,
            "/api/admin/plans",
            {"code": "unsafe", "name": "Unsafe", "price": bad_price},
            token,
        )
        assert response.status_code == 422

    def test_plan_rejects_boolean_integer_fields(self, auth):
        client, token, _ = auth
        response = post(
            client,
            "/api/admin/plans",
            {"code": "unsafe", "name": "Unsafe", "price": "10", "period_days": True},
            token,
        )
        assert response.status_code == 422

    def test_subscription_rejects_billing_managed_dates(self, auth, tenant, plan):
        client, token, _ = auth
        Subscription.objects.create(tenant=tenant, plan=plan, price=plan.price)
        response = post(
            client,
            f"/api/admin/subscriptions/{tenant.pk}",
            {"paid_through": timezone.now().isoformat()},
            token,
            method="patch",
        )
        assert response.status_code == 422

    def test_subscription_list_identifies_its_tenant(self, auth, tenant, plan):
        client, token, _ = auth
        Subscription.objects.create(tenant=tenant, plan=plan, price=plan.price)
        response = client.get(
            "/api/admin/subscriptions", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["tenant"] == {
            "id": tenant.pk,
            "org_name": tenant.org_name,
            "email": tenant.email,
        }

    def test_plan_change_approval_locks_and_copies_price(self, auth, tenant, plan):
        client, token, _ = auth
        old = SubscriptionPlan.objects.create(code="old", name="Old", price="10")
        sub = Subscription.objects.create(tenant=tenant, plan=old, price=old.price)
        change = PlanChangeRequest.objects.create(
            tenant=tenant, current_plan=old, requested_plan=plan
        )
        response = post(
            client,
            f"/api/admin/plan-changes/{change.pk}/approve",
            {"decision_note": "Approved"},
            token,
        )
        assert response.status_code == 200
        sub.refresh_from_db()
        assert sub.plan == plan
        assert sub.price == plan.price
        assert (
            post(
                client, f"/api/admin/plan-changes/{change.pk}/reject", {}, token
            ).status_code
            == 409
        )

    def test_payments_are_read_only_and_mask_external_id(self, auth, tenant):
        client, token, _ = auth
        Payment.objects.create(
            tenant=tenant,
            amount="5000",
            kind=Payment.Kind.TOPUP,
            source=Payment.Source.CLICK,
            external_id="provider-secret-1234",
            balance_after="855000",
        )
        response = client.get(
            "/api/admin/payments", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert response.status_code == 200
        row = response.json()["results"][0]
        assert row["external_id"] != "provider-secret-1234"
        assert (
            client.post(
                "/api/admin/payments", HTTP_AUTHORIZATION=f"Bearer {token}"
            ).status_code
            == 405
        )
        missing = client.post(
            f"/api/admin/tenants/{tenant.pk}/credit",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"
        with pytest.raises(ProtectedError):
            tenant.delete()
        assert Payment.objects.filter(tenant=tenant).exists()


class TestActivityAndMetadata:
    def test_heartbeat_payload_requires_specific_permission(self, tenant):
        user = User.objects.create_user(
            username="staff", password="password-long", is_staff=True
        )
        client = Client()
        login = post(
            client,
            "/api/admin/auth/login",
            {
                "username": "staff",
                "password": "password-long",
            },
        )
        token = login.json()["data"]["access_token"]
        key, _ = LicenseKey.issue(tenant)
        event = HeartbeatEvent.objects.create(
            license_key=key, payload={"secret": "detail"}
        )
        denied = client.get(
            "/api/admin/heartbeats?include_payload=true",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert denied.status_code == 403
        detail = client.get(
            f"/api/admin/heartbeats/{event.pk}", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert "payload" not in detail.json()["data"]

        permission = Permission.objects.get(codename="view_sensitive_heartbeat")
        user.user_permissions.add(permission)
        allowed = client.get(
            "/api/admin/heartbeats?include_payload=true",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        assert allowed.json()["results"][0]["payload"] == {"secret": "detail"}

    def test_overview_and_status_include_policy_metadata(self, auth, tenant, plan):
        client, token, _ = auth
        Subscription.objects.create(
            tenant=tenant, plan=plan, price=plan.price, period_days=plan.period_days
        )
        key, _ = LicenseKey.issue(tenant)
        HeartbeatEvent.objects.create(license_key=key, client_version="1.0.41")
        overview = client.get(
            "/api/admin/overview", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert overview.status_code == 200
        assert overview.json()["data"]["health_thresholds"]["online_minutes"] == 10
        status = client.get(
            "/api/admin/system/status", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert status.status_code == 200
        assert status.json()["data"]["database_reachable"] is True
        assert "heartbeat_ingestion_lag_seconds" in status.json()["data"]

    def test_secret_license_hash_is_never_serialized(self, auth, tenant):
        client, token, _ = auth
        key, _ = LicenseKey.issue(tenant)
        response = client.get(
            f"/api/admin/licenses/{key.pk}", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        serialized = json.dumps(response.json())
        assert key.key_hash not in serialized
        assert "key_hash" not in serialized

    def test_invalid_filters_and_cursors_return_json_422(self, auth):
        client, token, _ = auth
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        invalid_filter = client.get("/api/admin/licenses?tenant=not-an-id", **headers)
        assert invalid_filter.status_code == 422
        assert invalid_filter.json()["code"] == "validation_error"
        invalid_cursor = client.get("/api/admin/events?cursor=%%%", **headers)
        assert invalid_cursor.status_code == 422
        assert invalid_cursor.json()["code"] == "invalid_cursor"

    def test_invite_timestamps_are_serialized_in_utc(self, auth):
        client, token, _ = auth
        response = post(
            client,
            "/api/admin/invites",
            {
                "intended_email": "utc@example.com",
                "expires_at": "2026-09-01T05:00:00+05:00",
            },
            token,
        )
        assert response.status_code == 201
        assert response.json()["data"]["expires_at"] == "2026-09-01T00:00:00Z"

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=["https://dashboard.example.com"],
    )
    def test_production_cors_is_origin_allowlisted(self, auth):
        client, token, _ = auth
        denied = client.get(
            "/api/admin/overview",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_ORIGIN="https://evil.example",
        )
        assert "Access-Control-Allow-Origin" not in denied
        allowed = client.get(
            "/api/admin/overview",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_ORIGIN="https://dashboard.example.com",
        )
        assert allowed["Access-Control-Allow-Origin"] == "https://dashboard.example.com"
        preflight = client.options(
            "/api/admin/tenants",
            HTTP_ORIGIN="https://dashboard.example.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=(
                "authorization,content-type,idempotency-key"
            ),
        )
        assert preflight.status_code == 200
        assert "idempotency-key" in preflight["Access-Control-Allow-Headers"].lower()
