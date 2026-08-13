from django.contrib import admin
from django.utils import timezone

from licenses.models import ControlEvent
from tenants.models import InviteCode, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Tenant profile. Wallet credits are provider-only (Click/Payme)."""
    list_display = ('org_name', 'email', 'balance', 'created_at')
    search_fields = ('org_name', 'email', 'notes')
    readonly_fields = ('balance', 'created_at', 'updated_at')
    ordering = ('org_name',)

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
