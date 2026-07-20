"""Compatibility entry point for the unified Denarius Console."""

from blockchain_client.blockchain_client import (
    app,
    csrf_required,
    ensure_csrf_token,
    hash_password,
    login_required,
    node_base_url,
    verify_password,
)


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=5001, type=int, help='console port to listen on')
    args = parser.parse_args()
    node_base_url()
    app.run(host='127.0.0.1', port=args.port)
