from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.utils import timezone

from licenses.models import ControlEvent
from tenants.models import InviteCode, Tenant


class TopUpActionForm(ActionForm):
    """Collect the amount and audit note for a manual wallet top-up."""

    amount = forms.DecimalField(
        required=False,
        max_digits=14,
        decimal_places=2,
        label='Top-up amount',
        min_value=Decimal('0.01'),
    )
    note = forms.CharField(required=False, label='Note (optional)')


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Tenant profile with an audited manual wallet top-up action."""

    action_form = TopUpActionForm
    actions = ('top_up_balance',)
    list_display = ('org_name', 'email', 'balance', 'created_at')
    search_fields = ('org_name', 'email', 'notes')
    readonly_fields = ('balance', 'created_at', 'updated_at')
    ordering = ('org_name',)

    @admin.action(description='Top up balance (enter amount below)')
    def top_up_balance(self, request, queryset):
        """Credit selected tenants through the append-only billing ledger."""
        from billing.models import Payment
        from billing.services.billing import credit_balance

        raw_amount = (request.POST.get('amount') or '').strip()
        if not raw_amount:
            self.message_user(
                request,
                'Enter a value in “Top-up amount” before running this action.',
                level=messages.ERROR,
            )
            return

        try:
            amount = Decimal(raw_amount)
        except (InvalidOperation, TypeError):
            self.message_user(
                request,
                f'Invalid top-up amount: {raw_amount!r}.',
                level=messages.ERROR,
            )
            return

        note = (request.POST.get('note') or '').strip() or 'Manual admin top-up'
        credited = 0
        for tenant in queryset:
            try:
                credit_balance(
                    tenant,
                    amount,
                    source=Payment.Source.MANUAL,
                    actor=request.user if request.user.is_authenticated else None,
                    note=note,
                )
            except ValueError as exc:
                self.message_user(
                    request,
                    f'{tenant}: {exc}',
                    level=messages.ERROR,
                )
                continue
            credited += 1

        if credited:
            self.message_user(
                request,
                f'Credited {amount:.2f} to {credited} tenant(s). '
                'Any overdue subscription was settled automatically.',
                level=messages.SUCCESS,
            )

    def has_delete_permission(self, request, obj=None):
        # Tenants are durable account records. The control API intentionally
        # exposes suspension/revocation instead of destructive deletion.
        return False


@admin.register(InviteCode)
class InviteCodeAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'intended_org_name', 'intended_email',
        'consumed_at', 'expires_at', 'revoked_at', 'tenant',
    )
    list_filter = ('consumed_at', 'expires_at', 'revoked_at')
    search_fields = ('code', 'intended_email', 'intended_org_name', 'notes')
    readonly_fields = (
        'code', 'consumed_at', 'tenant', 'created_at', 'revoked_at', 'revoked_by',
    )
    fields = (
        'intended_org_name', 'intended_email', 'expires_at', 'notes',
        'code', 'consumed_at', 'tenant', 'created_at', 'revoked_at', 'revoked_by',
    )

    def save_model(self, request, obj, form, change):
        created = not change
        super().save_model(request, obj, form, change)
        if created:
            ControlEvent.objects.create(
                actor=request.user, action=ControlEvent.Action.INVITE_CREATE,
                tenant=obj.tenant,
                metadata={'invite_id': obj.pk, 'intended_email': obj.intended_email},
            )

    def has_delete_permission(self, request, obj=None):
        allowed = super().has_delete_permission(request, obj)
        return allowed and (obj is None or (not obj.consumed_at and not obj.revoked_at))

    def delete_model(self, request, obj):
        if obj.consumed_at or obj.revoked_at:
            return
        obj.revoked_at = timezone.now()
        obj.revoked_by = request.user
        obj.save(update_fields=['revoked_at', 'revoked_by'])
        ControlEvent.objects.create(
            actor=request.user, action=ControlEvent.Action.INVITE_REVOKE,
            tenant=obj.tenant, metadata={'invite_id': obj.pk},
        )

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)
