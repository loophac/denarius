# Inside triple quote is left by the original author. Check out REAME for more info on the refined info.
'''
title           : blockchain_client.py
description     : A blockchain client implemenation, with the following features
                  - Wallet generation using Ed25519 keys
                  - Generation of signed transactions
author          : Adil Moujahid
date_created    : 20180212
date_modified   : 20180309
version         : 0.3
usage           : python blockchain_client.py
                  python blockchain_client.py -p 8080
                  python blockchain_client.py --port 8080
python_version  : 3.6.1
Comments        : Wallet generation and transaction signature is based on [1]
References      : [1] https://github.com/julienr/ipynb_playground/blob/master/bitcoin/dumbcoin/dumbcoin.ipynb
'''

from decimal import Decimal, InvalidOperation
from pathlib import Path

import binascii
import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from flask import Flask, jsonify, request, render_template

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from denarius_protocol import (
    ATOMIC_UNITS,
    canonical_json_bytes,
    signed_transaction_id,
    transaction_signing_payload,
)
from denarius_crypto import (
    address_from_public_key,
    decrypt_wallet,
    generate_encrypted_wallet,
    wallet_public_metadata,
)

MAX_WALLET_FILE_BYTES = 64 * 1024


class Transaction:
    ATOMIC_UNITS = ATOMIC_UNITS

    def __init__(self, sender_address, sender_private_key, recipient_address, value, nonce):
        self.sender_address = sender_address
        self.sender_private_key = sender_private_key
        self.recipient_address = recipient_address
        self.value = self.parse_amount(value)
        try:
            self.nonce = int(str(nonce))
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid nonce') from exc
        if self.nonce < 0:
            raise ValueError('Invalid nonce')

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            binascii.unhexlify(self.sender_private_key)
        )
        public_key_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if address_from_public_key(public_key_bytes) != self.sender_address:
            raise ValueError('Private key does not match sender address')

    def parse_amount(self, value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError('Invalid amount')

        if not amount.is_finite() or amount <= 0:
            raise ValueError('Invalid amount')

        atomic_amount = amount * self.ATOMIC_UNITS
        if atomic_amount != atomic_amount.to_integral_value():
            raise ValueError('Invalid amount')

        return str(int(atomic_amount))

    def to_dict(self):
        return transaction_signing_payload(
            self.sender_address,
            self.recipient_address,
            self.value,
            self.nonce,
        )

    def canonical_transaction_bytes(self):
        return canonical_json_bytes(self.to_dict())

    def sign_transaction(self):
        """
        Sign transaction with private key
        """
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(binascii.unhexlify(self.sender_private_key))
        return binascii.hexlify(private_key.sign(self.canonical_transaction_bytes())).decode('ascii')

    def signed_data(self):
        signature = self.sign_transaction()
        return signature, signed_transaction_id(self.to_dict(), signature)


app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024

@app.route('/')
def index():
    return render_template('./index.html')


@app.route('/make/transaction')
def make_transaction():
    return render_template('./make_transaction.html')


@app.route('/view/transactions')
def view_transaction():
    return render_template('./view_transactions.html')


def uploaded_wallet_document():
    wallet_file = request.files.get('wallet_file')
    if wallet_file is None:
        raise ValueError('Encrypted wallet file is required')
    wallet_bytes = wallet_file.read(MAX_WALLET_FILE_BYTES + 1)
    if len(wallet_bytes) > MAX_WALLET_FILE_BYTES:
        raise ValueError('Encrypted wallet file is too large')
    try:
        document = json.loads(wallet_bytes.decode('utf8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Encrypted wallet file is invalid') from exc
    if not isinstance(document, dict):
        raise ValueError('Encrypted wallet file is invalid')
    return document


@app.route('/wallet/new', methods=['POST'])
def new_wallet():
    try:
        wallet_document = generate_encrypted_wallet(request.form.get('password', ''))
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    response = {
        'address': wallet_document['address'],
        'public_key': wallet_document['public_key'],
        'wallet': wallet_document,
        'filename': 'denarius-' + wallet_document['address'][:14] + '.denwallet',
    }
    return jsonify(response), 200


@app.route('/wallet/inspect', methods=['POST'])
def inspect_wallet():
    try:
        metadata = wallet_public_metadata(uploaded_wallet_document())
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    return jsonify(metadata), 200


@app.route('/generate/transaction', methods=['POST'])
def generate_transaction():
    try:
        wallet_data = decrypt_wallet(
            uploaded_wallet_document(),
            request.form.get('password', ''),
        )
        sender_address = wallet_data['address']
        sender_private_key = wallet_data['private_key']
        recipient_address = request.form['recipient_address']
        value = request.form['amount']
        nonce = request.form['nonce']

        transaction = Transaction(
            sender_address,
            sender_private_key,
            recipient_address,
            value,
            nonce,
        )
        signature, transaction_id = transaction.signed_data()
    except (binascii.Error, KeyError, ValueError):
        return jsonify({'message': 'Invalid transaction'}), 400

    response = {
        'transaction': transaction.to_dict(),
        'signature': signature,
        'transaction_id': transaction_id,
    }

    return jsonify(response), 200


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=8080, type=int, help='port to listen on')
    args = parser.parse_args()
    port = args.port

    app.run(host='127.0.0.1', port=port)


