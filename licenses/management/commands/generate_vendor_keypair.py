"""One-shot Ed25519 keypair bootstrap.

Run once at vendor setup, copy the printed values into the relevant
env vars, then NEVER run it again — re-running invalidates every
unlock file the previous key ever signed.

The private key should be stored in a hardware vault (YubiKey, AWS KMS,
1Password, etc.) and only loaded into LICENSE_VENDOR_PRIVATE_KEY for
the brief window needed to sign an unlock file. Even the control
center's production env shouldn't normally carry the private key.

The public key is embedded in every alpha_pos build via
LICENSE_VENDOR_PUBLIC_KEY — operators can't generate their own unlocks
because they don't have the private key.
"""
from django.core.management.base import BaseCommand

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


class Command(BaseCommand):
    help = (
        'Generate a fresh Ed25519 vendor keypair. Prints both halves to '
        'stdout exactly once. Re-running invalidates every unlock file '
        'the previous key signed.'
    )

    def handle(self, *args, **options):
        priv = Ed25519PrivateKey.generate()
        priv_hex = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        pub_hex = priv.public_key().public_bytes_raw().hex()

        self.stdout.write(self.style.WARNING(
            'Ed25519 vendor keypair generated. Copy these values now — '
            'they are NOT persisted anywhere.'
        ))
        self.stdout.write('')
        self.stdout.write('LICENSE_VENDOR_PRIVATE_KEY (vault — keep offline):')
        self.stdout.write(self.style.SUCCESS(priv_hex))
        self.stdout.write('')
        self.stdout.write('LICENSE_VENDOR_PUBLIC_KEY (embed in alpha_pos build):')
        self.stdout.write(self.style.SUCCESS(pub_hex))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'Reminder: re-running this command invalidates every unlock '
            'file the previous key signed.'
        ))
