from django.contrib import admin, messages

from licenses.models import ControlEvent, HeartbeatEvent, LicenseKey


@admin.register(LicenseKey)
class LicenseKeyAdmin(admin.ModelAdmin):
    """The dashboard's main surface — bulk admin actions are the primary
    way the vendor flips a tenant on/off. Every action writes a
    ControlEvent so the audit trail captures who did what when."""
    list_display = (
        'key_prefix', 'tenant', 'status', 'expires_at',
        'message', 'last_heartbeat', 'created_at',
    )
    list_filter = ('status',)
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

    actions = (
        'suspend_selected',
        'resume_selected',
        'clear_message',
    )

    def last_heartbeat(self, obj):
        """Most-recent HeartbeatEvent timestamp for the list view —
        cheap signal that the install is alive vs gone dark."""
        event = obj.heartbeat_events.order_by('-received_at').first()
        return event.received_at if event else '—'

    last_heartbeat.short_description = 'Last heartbeat'

    # -- bulk actions --------------------------------------------------------

    def _record_event(self, request, queryset, action, metadata=None):
        """Write a ControlEvent per LicenseKey for the audit trail."""
        ControlEvent.objects.bulk_create([
            ControlEvent(
                actor=request.user if request.user.is_authenticated else None,
                action=action,
                license_key=row,
                tenant=row.tenant,
                metadata=metadata or {},
            )
            for row in queryset
        ])

    @admin.action(description='Suspend selected licenses (kill switch fires next heartbeat)')
    def suspend_selected(self, request, queryset):
        # Materialise the matching rows BEFORE the update — otherwise
        # the queryset re-evaluates after .update() and `active` is
        # empty by the time we record events.
        active = list(queryset.filter(status=LicenseKey.Status.ACTIVE))
        if not active:
            self.message_user(request, 'No active licenses to suspend.', messages.INFO)
            return
        LicenseKey.objects.filter(pk__in=[r.pk for r in active]).update(
            status=LicenseKey.Status.SUSPENDED,
        )
        self._record_event(request, active, ControlEvent.Action.SUSPEND)
        self.message_user(
            request, f'Suspended {len(active)} licenses.', messages.SUCCESS,
        )

    @admin.action(description='Resume selected licenses')
    def resume_selected(self, request, queryset):
        suspended = list(queryset.filter(status=LicenseKey.Status.SUSPENDED))
        if not suspended:
            self.message_user(request, 'No suspended licenses to resume.', messages.INFO)
            return
        LicenseKey.objects.filter(pk__in=[r.pk for r in suspended]).update(
            status=LicenseKey.Status.ACTIVE,
        )
        self._record_event(request, suspended, ControlEvent.Action.RESUME)
        self.message_user(
            request, f'Resumed {len(suspended)} licenses.', messages.SUCCESS,
        )

    @admin.action(description='Clear banner message')
    def clear_message(self, request, queryset):
        had_message = list(queryset.exclude(message=''))
        if not had_message:
            self.message_user(request, 'No messages to clear.', messages.INFO)
            return
        LicenseKey.objects.filter(pk__in=[r.pk for r in had_message]).update(message='')
        self._record_event(
            request, had_message, ControlEvent.Action.SET_MESSAGE,
            metadata={'new_message': ''},
        )
        self.message_user(
            request, f'Cleared message on {len(had_message)} licenses.', messages.SUCCESS,
        )

    # -- audit on individual saves ------------------------------------------

    def save_model(self, request, obj, form, change):
        """Catch admin-form edits (status flip, message text, expires_at
        change) so they also write ControlEvents — the bulk actions cover
        the common case but ad-hoc edits should be just as auditable."""
        if change and obj.pk:
            prev = LicenseKey.objects.get(pk=obj.pk)
            self._diff_and_record(request, prev, obj)
        super().save_model(request, obj, form, change)

    def _diff_and_record(self, request, before, after):
        actor = request.user if request.user.is_authenticated else None
        if before.status != after.status:
            mapping = {
                LicenseKey.Status.SUSPENDED: ControlEvent.Action.SUSPEND,
                LicenseKey.Status.ACTIVE: ControlEvent.Action.RESUME,
                LicenseKey.Status.REVOKED: ControlEvent.Action.REVOKE,
            }
            action = mapping.get(after.status, ControlEvent.Action.RESUME)
            ControlEvent.objects.create(
                actor=actor, action=action,
                license_key=after, tenant=after.tenant,
                metadata={'from': before.status, 'to': after.status},
            )
        if before.message != after.message:
            ControlEvent.objects.create(
                actor=actor, action=ControlEvent.Action.SET_MESSAGE,
                license_key=after, tenant=after.tenant,
                metadata={'old': before.message, 'new': after.message},
            )
        if before.expires_at != after.expires_at:
            ControlEvent.objects.create(
                actor=actor, action=ControlEvent.Action.EXTEND_EXPIRY,
                license_key=after, tenant=after.tenant,
                metadata={
                    'old_expires_at': before.expires_at.isoformat() if before.expires_at else None,
                    'new_expires_at': after.expires_at.isoformat() if after.expires_at else None,
                },
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
