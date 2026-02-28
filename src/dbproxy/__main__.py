"""Entry point: uv run -m dbproxy"""

from __future__ import annotations

import argparse
import sys

from .config import ProxyConfig
from .logging_config import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Synix Claude Proxy")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 443)")
    parser.add_argument("--passthrough", action="store_true", help="Passthrough mode (no buffer logic)")
    parser.add_argument("--log-level", default=None, help="Log level (default: DEBUG)")
    parser.add_argument("--setup-tls", action="store_true", help="Generate TLS certs and install CA, then exit")
    parser.add_argument("--setup-hosts", action="store_true", help="Add /etc/hosts entry, then exit")
    args = parser.parse_args()

    config = ProxyConfig()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.passthrough:
        config.passthrough = True
    if args.log_level:
        config.log_level = args.log_level

    setup_logging(config.log_dir, config.log_level)

    if args.setup_tls:
        from .tls import generate_certs, install_ca_system_trust
        ca_path, cert_path, key_path = generate_certs(config.tls_ca_dir)
        print(f"CA cert:     {ca_path}")
        print(f"Server cert: {cert_path}")
        print(f"Server key:  {key_path}")
        install_ca_system_trust(ca_path)
        print("CA installed in system trust store.")
        sys.exit(0)

    if args.setup_hosts:
        _setup_hosts()
        sys.exit(0)

    from .server import run_server
    run_server(config)


def _setup_hosts() -> None:
    """Add api.anthropic.com → 127.0.0.1 to /etc/hosts."""
    hosts_line = "127.0.0.1 api.anthropic.com"
    hosts_path = "/etc/hosts"

    with open(hosts_path) as f:
        content = f.read()

    if "api.anthropic.com" in content:
        print(f"{hosts_path} already contains api.anthropic.com entry")
        return

    with open(hosts_path, "a") as f:
        f.write(f"\n# Synix Claude Proxy\n{hosts_line}\n")
    print(f"Added '{hosts_line}' to {hosts_path}")


if __name__ == "__main__":
    main()
