"""Provide a verifying SSL context for urllib calls.

PyInstaller bundles often ship a Python whose OpenSSL has compiled-in CA
paths that don't exist on the target machine, which surfaces as
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` when
the app fetches releases from GitHub. We prefer certifi's vendored bundle
(installed by the build scripts) and fall back to well-known system paths
so running from source still works without certifi installed.
"""
from __future__ import annotations

import os
import ssl


_SYSTEM_CA_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
)


def _certifi_path():
    try:
        import certifi
        path = certifi.where()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None


def _system_ca_path():
    env = os.environ.get("SSL_CERT_FILE")
    if env and os.path.isfile(env):
        return env
    for path in _SYSTEM_CA_PATHS:
        if os.path.isfile(path):
            return path
    return None


def get_ssl_context():
    path = _certifi_path() or _system_ca_path()
    if path:
        return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()
