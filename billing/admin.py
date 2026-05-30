from django.contrib import admin

from billing.models import Payment, Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    """Set the plan price/period/warn window. `paid_through` is advanced by
    the billing service (charges from the wallet) — it's read-only here so
    the operator can't hand-edit a paid-through date and bypass billing."""
    list_display = (
        'tenant', 'price', 'period_days', 'status',
        'paid_through', 'warn_days', 'grace_days', 'last_charged_at',
    )
    list_filter = ('status',)
    search_fields = ('tenant__org_name', 'tenant__email')
    autocomplete_fields = ('tenant',)
    readonly_fields = (
        'paid_through', 'last_charged_at',
        'last_warn_sent_at', 'last_grace_sent_at', 'last_lockout_sent_at',
        'created_at', 'updated_at',
    )
    fieldsets = (
        ('Plan', {
            'fields': (
                'tenant', 'price', 'period_days',
                'warn_days', 'grace_days', 'status',
            ),
        }),
        ('Billing state (managed automatically)', {
            'fields': ('paid_through', 'last_charged_at'),
        }),
        ('Warning emails (sent by bill_subscriptions)', {
            'fields': (
                'last_warn_sent_at', 'last_grace_sent_at', 'last_lockout_sent_at',
            ),
            'classes': ('collapse',),
        }),
        ('Lifecycle', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Append-only money ledger. Read-only — rows are written by the billing
    service (top-ups, subscription charges), never edited by hand."""
    list_display = (
        'created_at', 'tenant', 'kind', 'source',
        'amount', 'balance_after', 'actor', 'note',
    )
    list_filter = ('kind', 'source')
    search_fields = ('tenant__org_name', 'tenant__email', 'external_id', 'note')
    readonly_fields = (
        'tenant', 'amount', 'kind', 'source', 'external_id',
        'balance_after', 'actor', 'note', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
