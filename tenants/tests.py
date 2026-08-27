from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

from billing.models import Payment
from tenants.models import Tenant


def test_admin_can_top_up_tenant_balance_with_audited_ledger_entry(client, db):
    operator = get_user_model().objects.create_superuser(
        username='operator',
        email='operator@example.com',
        password='not-used-by-force-login',
    )
    tenant = Tenant.objects.create(
        org_name='Shehir Cafe',
        email='shehir@example.com',
        balance=Decimal('125000.00'),
    )
    client.force_login(operator)

    response = client.post(
        reverse('admin:tenants_tenant_changelist'),
        {
            'action': 'top_up_balance',
            '_selected_action': [str(tenant.pk)],
            'select_across': '0',
            'amount': '375000.00',
            'note': 'Shehir Cafe launch credit',
        },
    )

    assert response.status_code == 302
    tenant.refresh_from_db()
    assert tenant.balance == Decimal('500000.00')

    payment = Payment.objects.get(tenant=tenant)
    assert payment.kind == Payment.Kind.TOPUP
    assert payment.source == Payment.Source.MANUAL
    assert payment.amount == Decimal('375000.00')
    assert payment.balance_after == Decimal('500000.00')
    assert payment.actor == operator
    assert payment.note == 'Shehir Cafe launch credit'


def test_admin_top_up_requires_a_positive_amount(client, db):
    operator = get_user_model().objects.create_superuser(
        username='operator',
        email='operator@example.com',
        password='not-used-by-force-login',
    )
    tenant = Tenant.objects.create(
        org_name='Shehir Cafe',
        email='shehir@example.com',
    )
    client.force_login(operator)

    response = client.post(
        reverse('admin:tenants_tenant_changelist'),
        {
            'action': 'top_up_balance',
            '_selected_action': [str(tenant.pk)],
            'select_across': '0',
            'amount': '-1.00',
        },
    )

    assert response.status_code == 302
    tenant.refresh_from_db()
    assert tenant.balance == Decimal('0.00')
    assert not Payment.objects.filter(tenant=tenant).exists()
