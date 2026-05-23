"""Produce a base64 perpetual-unlock file signed by the vendor key.

Intended to be run on an air-gapped machine: read the private key from
the vault into LICENSE_VENDOR_PRIVATE_KEY env var, run this command,
copy the printed base64 string into the file the vendor publishes on
their status page.

The signed payload is "PERPETUAL_UNLOCK_v1|<signed_at>". Any alpha_pos
install with the matching public key embedded will accept it via
`POST /api/licensing/unlock`. The file unlocks EVERY install — it is
not tenant-bound by design (the whole point is "vendor disappeared,
everyone needs to be able to use this").
"""
import base64
import binascii
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PAYLOAD_STRING = 'PERPETUAL_UNLOCK_v1'


class Command(BaseCommand):
    help = (
        'Sign a perpetual-unlock file with the vendor Ed25519 private key. '
        'Reads LICENSE_VENDOR_PRIVATE_KEY (hex). Prints the base64-wrapped '
        'JSON blob to stdout.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--signed-at', default=None,
            help='Override the signed_at timestamp (ISO 8601). Default: now.',
        )

    def handle(self, *args, **options):
        priv_hex = getattr(settings, 'LICENSE_VENDOR_PRIVATE_KEY', '') or ''
        if not priv_hex:
            raise CommandError(
                'LICENSE_VENDOR_PRIVATE_KEY is not set. Load it from your '
                'vault before running this command — and remove it from the '
                'environment immediately afterward.'
            )
        try:
            priv_bytes = binascii.unhexlify(priv_hex)
        except (binascii.Error, ValueError):
            raise CommandError('LICENSE_VENDOR_PRIVATE_KEY is not valid hex.')
        if len(priv_bytes) != 32:
            raise CommandError(
                f'LICENSE_VENDOR_PRIVATE_KEY must be 32 bytes (got {len(priv_bytes)}).'
            )

        signed_at = options['signed_at'] or timezone.now().isoformat()
        message = f'{PAYLOAD_STRING}|{signed_at}'.encode('utf-8')
        priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        sig = priv.sign(message)

        blob = {
            'payload': PAYLOAD_STRING,
            'signed_at': signed_at,
            'signature': binascii.hexlify(sig).decode('ascii'),
        }
        wrapped = base64.b64encode(json.dumps(blob).encode('utf-8')).decode('ascii')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('PERPETUAL UNLOCK FILE'))
        self.stdout.write(f'signed_at: {signed_at}')
        self.stdout.write('')
        self.stdout.write(wrapped)
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'Publish this string on your status page. Any alpha_pos install '
            'can paste it into POST /api/licensing/unlock to permanently '
            'disable its kill switch.'
        ))
