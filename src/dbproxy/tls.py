"""TLS certificate generation and SSL context setup.

Generates a CA + server certificate for api.anthropic.com so the proxy
can terminate TLS locally. Uses trustme for programmatic cert generation.
"""

from __future__ import annotations

import os
import ssl

import structlog
import trustme

log = structlog.get_logger()

CA_CERT_FILE = "ca.pem"
SERVER_CERT_FILE = "server.pem"
SERVER_KEY_FILE = "server.key"


def generate_certs(ca_dir: str) -> tuple[str, str, str]:
    """Generate CA + server cert for api.anthropic.com if not already present.

    Returns (ca_path, cert_path, key_path).
    """
    os.makedirs(ca_dir, exist_ok=True)
    ca_path = os.path.join(ca_dir, CA_CERT_FILE)
    cert_path = os.path.join(ca_dir, SERVER_CERT_FILE)
    key_path = os.path.join(ca_dir, SERVER_KEY_FILE)

    if os.path.exists(ca_path) and os.path.exists(cert_path) and os.path.exists(key_path):
        log.info("tls_certs_exist", ca_dir=ca_dir)
        return ca_path, cert_path, key_path

    log.info("tls_generating_certs", ca_dir=ca_dir)
    ca = trustme.CA()
    server_cert = ca.issue_cert("api.anthropic.com")

    # Write CA cert
    ca.cert_pem.write_to_path(ca_path)

    # Write server cert + key
    # trustme stores cert chain as multiple blobs, key as one
    with open(cert_path, "wb") as f:
        for blob in server_cert.cert_chain_pems:
            f.write(blob.bytes())

    server_cert.private_key_pem.write_to_path(key_path)

    log.info("tls_certs_generated", ca_path=ca_path, cert_path=cert_path, key_path=key_path)
    return ca_path, cert_path, key_path


def create_server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Create an SSL context for the proxy server."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def install_ca_system_trust(ca_path: str) -> None:
    """Install the CA certificate into the system trust store.

    Requires root. Copies CA cert to /usr/local/share/ca-certificates/
    and runs update-ca-certificates.
    """
    import shutil
    import subprocess

    dest = "/usr/local/share/ca-certificates/dbproxy-ca.crt"
    shutil.copy2(ca_path, dest)
    subprocess.run(["update-ca-certificates"], check=True)
    log.info("tls_ca_installed_system", dest=dest)
