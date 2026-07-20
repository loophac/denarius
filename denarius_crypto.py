import binascii
import hashlib
import json
import os
from collections import OrderedDict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from denarius_protocol import canonical_json_bytes


WALLET_FORMAT = 'denarius-wallet'
WALLET_VERSION = 1
WALLET_CIPHER = 'aes-256-gcm'
WALLET_KDF = 'scrypt'
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 32
MIN_PASSWORD_LENGTH = 10

WALLET_FIELDS = (
    'format',
    'version',
    'address',
    'public_key',
    'cipher',
    'kdf',
    'kdf_n',
    'kdf_r',
    'kdf_p',
    'salt',
    'nonce',
    'ciphertext',
)
WALLET_AAD_FIELDS = WALLET_FIELDS[:-1]


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


def _private_key_bytes(private_key):
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_key_bytes(private_key):
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError('Wallet password must be at least 10 characters')
    return password.encode('utf8')


def _derive_wallet_key(password, salt):
    return Scrypt(
        salt=salt,
        length=SCRYPT_LENGTH,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(_validate_password(password))


def wallet_public_metadata(wallet_document):
    if not isinstance(wallet_document, dict) or set(wallet_document) != set(WALLET_FIELDS):
        raise ValueError('Invalid Denarius wallet document')
    if wallet_document.get('format') != WALLET_FORMAT or wallet_document.get('version') != WALLET_VERSION:
        raise ValueError('Unsupported Denarius wallet format')
    if wallet_document.get('cipher') != WALLET_CIPHER or wallet_document.get('kdf') != WALLET_KDF:
        raise ValueError('Unsupported Denarius wallet encryption')
    if (
        wallet_document.get('kdf_n') != SCRYPT_N
        or wallet_document.get('kdf_r') != SCRYPT_R
        or wallet_document.get('kdf_p') != SCRYPT_P
    ):
        raise ValueError('Unsupported Denarius wallet key derivation settings')

    public_key_hex = wallet_document.get('public_key')
    try:
        public_key_bytes = binascii.unhexlify(public_key_hex)
        if len(public_key_bytes) != 32:
            raise ValueError('Invalid wallet public key length')
        ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError('Invalid wallet public key') from exc
    address = address_from_public_key(public_key_bytes)
    if address != wallet_document.get('address'):
        raise ValueError('Wallet address does not match its public key')

    for field, expected_length in (('salt', 32), ('nonce', 24)):
        value = wallet_document.get(field)
        if not isinstance(value, str) or len(value) != expected_length:
            raise ValueError('Invalid wallet encryption metadata')
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError('Invalid wallet encryption metadata') from exc

    ciphertext = wallet_document.get('ciphertext')
    if not isinstance(ciphertext, str) or len(ciphertext) < 32:
        raise ValueError('Invalid wallet ciphertext')
    try:
        bytes.fromhex(ciphertext)
    except ValueError as exc:
        raise ValueError('Invalid wallet ciphertext') from exc

    return {
        'address': address,
        'public_key': public_key_hex,
    }


def encrypt_private_key(private_key_bytes, password):
    if not isinstance(private_key_bytes, bytes) or len(private_key_bytes) != 32:
        raise ValueError('Invalid Ed25519 private key')
    try:
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except ValueError as exc:
        raise ValueError('Invalid Ed25519 private key') from exc

    public_key_bytes = _public_key_bytes(private_key)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    metadata = OrderedDict({
        'format': WALLET_FORMAT,
        'version': WALLET_VERSION,
        'address': address_from_public_key(public_key_bytes),
        'public_key': public_key_bytes.hex(),
        'cipher': WALLET_CIPHER,
        'kdf': WALLET_KDF,
        'kdf_n': SCRYPT_N,
        'kdf_r': SCRYPT_R,
        'kdf_p': SCRYPT_P,
        'salt': salt.hex(),
        'nonce': nonce.hex(),
    })
    plaintext = canonical_json_bytes({'private_key': private_key_bytes.hex()})
    ciphertext = AESGCM(_derive_wallet_key(password, salt)).encrypt(
        nonce,
        plaintext,
        canonical_json_bytes(metadata),
    )
    metadata['ciphertext'] = ciphertext.hex()
    return metadata


def generate_encrypted_wallet(password):
    private_key = ed25519.Ed25519PrivateKey.generate()
    return encrypt_private_key(_private_key_bytes(private_key), password)


def decrypt_wallet(wallet_document, password):
    metadata = wallet_public_metadata(wallet_document)
    aad = OrderedDict((field, wallet_document[field]) for field in WALLET_AAD_FIELDS)
    salt = bytes.fromhex(wallet_document['salt'])
    nonce = bytes.fromhex(wallet_document['nonce'])
    ciphertext = bytes.fromhex(wallet_document['ciphertext'])
    try:
        plaintext = AESGCM(_derive_wallet_key(password, salt)).decrypt(
            nonce,
            ciphertext,
            canonical_json_bytes(aad),
        )
        private_key_payload = json.loads(plaintext.decode('utf8'))
        if set(private_key_payload) != {'private_key'}:
            raise ValueError('Invalid wallet private key payload')
        private_key_bytes = bytes.fromhex(private_key_payload['private_key'])
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError('Invalid wallet password or damaged wallet file') from exc

    public_key_bytes = _public_key_bytes(private_key)
    if public_key_bytes.hex() != metadata['public_key']:
        raise ValueError('Wallet private key does not match its public key')
    return {
        'address': metadata['address'],
        'public_key': metadata['public_key'],
        'private_key': private_key_bytes.hex(),
    }
