import binascii
import hashlib

from cryptography.hazmat.primitives.asymmetric import ed25519


def address_from_public_key(public_key_bytes):
    public_key_hex = binascii.hexlify(public_key_bytes).decode('ascii')
    checksum = hashlib.sha256(('DENARIUS:' + public_key_hex).encode('ascii')).hexdigest()[:8]
    return 'dn' + public_key_hex + checksum


def public_key_from_address(address):
    if not isinstance(address, str) or len(address) != 74 or not address.startswith('dn'):
        return None

    public_key_hex = address[2:66]
    checksum = address[66:]
    expected_checksum = hashlib.sha256(
        ('DENARIUS:' + public_key_hex).encode('ascii')
    ).hexdigest()[:8]
    if checksum != expected_checksum:
        return None

    try:
        public_key_bytes = binascii.unhexlify(public_key_hex)
        ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        return public_key_bytes
    except (ValueError, TypeError, binascii.Error):
        return None
