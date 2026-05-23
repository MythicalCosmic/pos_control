import os

import django
import pytest

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pos_control_center.settings')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('SECRET_KEY', 'pytest-control-center-key')

django.setup()


@pytest.fixture
def invite_code(db):
    """Unbound invite code — any email / org name can redeem it."""
    from tenants.models import InviteCode
    return InviteCode.objects.create()


@pytest.fixture
def bound_invite_code(db):
    """Invite pre-bound to a specific customer — redemption must match."""
    from tenants.models import InviteCode
    return InviteCode.objects.create(
        intended_email='plov@example.com',
        intended_org_name='Plov Plus',
    )
