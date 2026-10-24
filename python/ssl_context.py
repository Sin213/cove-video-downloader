"""Verifying urllib contexts with application-local supplemental Windows trust.

Windows defaults are loaded first, then the packaged Mozilla bundle. An explicit
SSL_CERT_FILE is exclusive. No Windows stores are modified. Python's Windows
loader reads ROOT and CA, not Disallowed; this union does not enforce enterprise
distrust or revocation, and additionally trusts Mozilla roots Windows lacks or
policy-distrusts. Non-Windows trust selection retains its original behavior.
"""
from __future__ import annotations

import os
import ssl
import sys
import hashlib
import threading
import re
import base64


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


def _legacy_context():
    path = _certifi_path() or _system_ca_path()
    if path:
        return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


class SslTrustConfigError(Exception):
    """Invalid explicit or supplemental CA configuration (no paths in messages)."""


_diagnostic = threading.local()


def get_trust_diagnostic():
    """Return sanitized, per-thread trust metadata, including failed attempts."""
    value = dict(getattr(_diagnostic, "value", {"sources": []}))
    value["sources"] = list(value["sources"])
    return value


def _is_windows():
    return sys.platform == "win32"


def _windows_bundle_path():
    # Never resolve a bundle relative to the working directory.
    if os.path.isabs(sys.executable):
        packaged = os.path.join(os.path.dirname(sys.executable), "certifi", "cacert.pem")
        if _bundle_present(packaged):
            return packaged
    try:
        import certifi
    except ImportError:
        return None
    except Exception:
        raise SslTrustConfigError("certifi-bundle: unable to import CA provider") from None
    try:
        where = getattr(certifi, "where", None)
        if not callable(where):
            return None
        path = where()
    except (ImportError, AttributeError):
        return None
    except Exception:
        raise SslTrustConfigError("certifi-bundle: unable to query CA provider") from None
    try:
        if path and not os.path.isabs(path):
            raise SslTrustConfigError("certifi-bundle: CA path must be absolute")
        return path if path and _bundle_present(path) else None
    except SslTrustConfigError:
        raise
    except Exception:
        raise SslTrustConfigError("certifi-bundle: invalid CA provider path") from None


def _bundle_present(path):
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        # Preserve inaccessible candidates so reading them produces a diagnostic;
        # exists()/isfile() would incorrectly permit a broader-trust fallback.
        return True
    return True


def _pem_certificates(data):
    armor = rb'(CERTIFICATE|X509 CERTIFICATE|TRUSTED CERTIFICATE)'
    blocks = list(re.finditer(rb'-----BEGIN ' + armor + rb'-----(.*?)-----END \1-----', data, re.S))
    markers = re.findall(rb'-----(?:BEGIN|END) ' + armor + rb'-----', data)
    if not blocks or len(markers) != 2 * len(blocks):
        raise ValueError('missing or truncated certificate block')
    result = []
    for block in blocks:
        der = base64.b64decode(re.sub(rb'\s+', b'', block[2]), validate=True)
        # cadata cannot retain OpenSSL X509_AUX trust/reject constraints. Never
        # silently discard those constraints while normalizing TRUSTED armor.
        if len(der) < 2 or der[0] != 0x30:
            raise ValueError('invalid certificate DER')
        length_bytes = der[1] & 0x7f if der[1] & 0x80 else 0
        header = 2 + length_bytes
        length = int.from_bytes(der[2:header], 'big') if length_bytes else der[1]
        if block[1] == b'TRUSTED CERTIFICATE' and header + length < len(der):
            raise ValueError('unsupported auxiliary trust constraints')
        if length_bytes > 4 or header + length != len(der):
            raise ValueError('invalid certificate or unsupported auxiliary trust constraints')
        result.append(b'-----BEGIN CERTIFICATE-----\n' + base64.b64encode(der) + b'\n-----END CERTIFICATE-----\n')
    return b''.join(result).decode('ascii')


def _inspect_ca(path, label):
    try:
        with open(path, "rb") as source:
            data = source.read()
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        pem = _pem_certificates(data)
        probe.load_verify_locations(cadata=pem)
        count = probe.cert_store_stats()["x509_ca"]
        if not count:
            raise ValueError("zero CA certificates")
    except OSError as error:
        reason = "missing" if isinstance(error, FileNotFoundError) else "unreadable or malformed"
        raise SslTrustConfigError(f"{label}: {reason} CA file") from None
    except (ValueError, UnicodeError) as error:
        if str(error) == 'unsupported auxiliary trust constraints':
            raise SslTrustConfigError(f"{label}: unsupported auxiliary trust constraints") from None
        raise SslTrustConfigError(f"{label}: malformed or zero-cert CA file") from None
    return pem, {"sha256": hashlib.sha256(data).hexdigest(), "cert_count": count}


def get_ssl_context():
    if not _is_windows():
        return _legacy_context()
    _diagnostic.value = {"sources": []}
    explicit = os.environ.get("SSL_CERT_FILE")
    if explicit:
        _diagnostic.value = {"sources": ["ssl_cert_file"]}
        data, metadata = _inspect_ca(explicit, "SSL_CERT_FILE")
        _diagnostic.value.update(metadata)
        try:
            context = ssl.create_default_context(cadata=data)
        except (OSError, ValueError):
            raise SslTrustConfigError("SSL_CERT_FILE: unable to load configured CA file") from None
    else:
        context = ssl.create_default_context()
        _diagnostic.value = {"sources": ["windows-system"]}
        bundle = _windows_bundle_path()
        if bundle is None:
            _diagnostic.value["sources"].append("bundle-missing")
            _diagnostic.value["status"] = "supplemental bundle MISSING"
        else:
            _diagnostic.value["sources"].append("certifi-bundle")
            data, metadata = _inspect_ca(bundle, "certifi-bundle")
            _diagnostic.value.update(metadata)
            try:
                context.load_verify_locations(cadata=data)
            except (OSError, ValueError):
                raise SslTrustConfigError("certifi-bundle: unable to load supplemental CA file") from None
    if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise SslTrustConfigError("TLS context must require chain and hostname verification")
    return context
