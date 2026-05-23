from django.contrib import admin

from licenses.models import LicenseKey


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
