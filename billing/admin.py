from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from billing.models import (
    Payment, PlanChangeRequest, Subscription, SubscriptionPlan,
)
from billing.services.billing import bind_plan_to_subscription
from licenses.models import ControlEvent


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    """Catalog of pricing tiers the wizard offers to customers. Edits to a
    plan don't retroactively re-bill existing subscribers — they'll see the
    new price on their next plan change. To change a single tenant's price
    without affecting others, edit their Subscription row directly."""
    list_display = (
        'code', 'name', 'price', 'period_days',
        'warn_days', 'grace_days', 'is_active', 'sort_order',
    )
    list_filter = ('is_active',)
    search_fields = ('code', 'name', 'description')
    ordering = ('sort_order', 'price')

    def has_delete_permission(self, request, obj=None):
        # Catalog rows are retired with is_active=False so historical
        # subscriptions and plan-change requests keep their source plan.
        return False


@admin.register(PlanChangeRequest)
class PlanChangeRequestAdmin(admin.ModelAdmin):
    """Customer-filed plan-change requests. Approve here to swap the
    tenant's Subscription onto the new plan; the change applies at the
    next charge (current period stays paid)."""
    list_display = (
        'tenant', 'current_plan', 'requested_plan',
        'status', 'requested_at', 'decided_at', 'decided_by',
    )
    list_filter = ('status', 'requested_plan')
    search_fields = ('tenant__org_name', 'tenant__email', 'note')
    readonly_fields = (
        'tenant', 'current_plan', 'requested_plan',
        'requested_at', 'decided_at', 'decided_by',
    )
    actions = ('approve_selected', 'reject_selected')

    def has_add_permission(self, request):
        # Requests come in via /api/v1/plan-change. The admin should never
        # create them by hand — that bypasses the customer's consent.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Approve plan change')
    def approve_selected(self, request, queryset):
        applied = 0
        for req in queryset.filter(status=PlanChangeRequest.Status.PENDING):
            with transaction.atomic():
                req = PlanChangeRequest.objects.select_for_update().get(pk=req.pk)
                if req.status != PlanChangeRequest.Status.PENDING:
                    continue
                sub = Subscription.objects.select_for_update().filter(
                    tenant_id=req.tenant_id,
                ).first()
                if sub is None:
                    # Tenant lost its subscription somehow — recreate from plan.
                    sub = Subscription(tenant=req.tenant)
                bind_plan_to_subscription(sub, req.requested_plan)
                sub.save()

                req.status = PlanChangeRequest.Status.APPROVED
                req.decided_at = timezone.now()
                req.decided_by = request.user
                req.save(update_fields=['status', 'decided_at', 'decided_by'])
                ControlEvent.objects.create(
                    actor=request.user,
                    action=ControlEvent.Action.PLAN_CHANGE_APPROVE,
                    tenant=req.tenant,
                    metadata={
                        'plan_change_id': req.pk,
                        'requested_plan_id': req.requested_plan_id,
                    },
                )
                applied += 1
        self.message_user(
            request,
            f'Approved {applied} plan change(s); takes effect on next charge.',
            messages.SUCCESS,
        )

    @admin.action(description='Reject plan change')
    def reject_selected(self, request, queryset):
        # Rebuild from IDs before locking. The admin changelist queryset may
        # contain nullable select_related joins, which PostgreSQL cannot lock
        # with FOR UPDATE.
        selected_ids = list(queryset.values_list('pk', flat=True))
        with transaction.atomic():
            pending = list(
                PlanChangeRequest.objects.select_for_update()
                .select_related('tenant')
                .filter(
                    pk__in=selected_ids,
                    status=PlanChangeRequest.Status.PENDING,
                )
            )
            rejected = PlanChangeRequest.objects.filter(
                pk__in=[row.pk for row in pending],
            ).update(
                status=PlanChangeRequest.Status.REJECTED,
                decided_at=timezone.now(),
                decided_by=request.user,
            )
            ControlEvent.objects.bulk_create([
                ControlEvent(
                    actor=request.user,
                    action=ControlEvent.Action.PLAN_CHANGE_REJECT,
                    tenant=row.tenant,
                    metadata={
                        'plan_change_id': row.pk,
                        'requested_plan_id': row.requested_plan_id,
                    },
                ) for row in pending
            ])
        self.message_user(
            request, f'Rejected {rejected} plan change(s).', messages.SUCCESS,
        )


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

    def has_delete_permission(self, request, obj=None):
        return False


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

    def has_delete_permission(self, request, obj=None):
        return False
