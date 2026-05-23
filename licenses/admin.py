from django.contrib import admin

from licenses.models import ControlEvent, HeartbeatEvent, LicenseKey


@admin.register(LicenseKey)
class LicenseKeyAdmin(admin.ModelAdmin):
    """The dashboard's main surface. Custom admin actions
    (suspend/resume/extend/banner-message) come in a follow-up commit;
    this first commit just gives the operator visibility."""
    list_display = (
        'key_prefix', 'tenant', 'status', 'expires_at',
        'message', 'created_at',
    )
    list_filter = ('status', 'expires_at')
    search_fields = ('tenant__org_name', 'tenant__email', 'key_prefix')
    readonly_fields = (
        'key_hash', 'key_prefix', 'created_at', 'revoked_at',
    )
    fieldsets = (
        ('Tenant', {'fields': ('tenant',)}),
        ('Key (read-only — issued at registration)', {
            'fields': ('key_prefix', 'key_hash'),
        }),
        ('State', {
            'fields': ('status', 'expires_at', 'message', 'notes'),
        }),
        ('Lifecycle', {
            'fields': ('created_at', 'revoked_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(HeartbeatEvent)
class HeartbeatEventAdmin(admin.ModelAdmin):
    """High-volume table — list view stays narrow on purpose. Use the
    LicenseKey detail page to find a tenant's recent activity rather
    than scrolling this list."""
    list_display = (
        'received_at', 'license_key', 'client_version',
        'branch_id', 'fingerprint', 'ip',
    )
    list_filter = ('branch_id', 'client_version')
    search_fields = ('license_key__key_prefix', 'fingerprint', 'ip')
    readonly_fields = (
        'license_key', 'ack_id', 'received_at', 'ip',
        'client_version', 'branch_id', 'fingerprint', 'payload',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ControlEvent)
class ControlEventAdmin(admin.ModelAdmin):
    """Audit trail. Read-only — control events should never be edited."""
    list_display = ('created_at', 'action', 'actor', 'license_key', 'tenant')
    list_filter = ('action', 'actor')
    search_fields = (
        'license_key__key_prefix', 'tenant__org_name', 'tenant__email',
    )
    readonly_fields = (
        'actor', 'action', 'license_key', 'tenant', 'metadata', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
