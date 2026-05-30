"""Add grace_days cushion + warning-email throttles to Subscription.

`grace_days` keeps a tenant ACTIVE for N days after paid_through lapses so a
missed renewal doesn't brick a busy restaurant the instant the clock ticks
past. The three timestamps throttle the daily warning email sender in
`bill_subscriptions` so each tenant gets at most one nudge per state.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_clicktransaction'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='grace_days',
            field=models.PositiveIntegerField(
                default=3,
                help_text='Days of soft cushion after paid_through before lockout.',
            ),
        ),
        migrations.AddField(
            model_name='subscription',
            name='last_warn_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscription',
            name='last_grace_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='subscription',
            name='last_lockout_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
