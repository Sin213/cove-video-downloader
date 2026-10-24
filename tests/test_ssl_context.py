"""Synthetic loopback-only TLS tests: no internet or DNS."""
import contextlib
import hashlib
import json
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import ssl_context as trust
import backend

FIX = Path(__file__).parent / 'fixtures/tls'
ORIGINAL_BUNDLE_PATH = trust._windows_bundle_path


@pytest.fixture(autouse=True)
def windows_trust(monkeypatch, request):
    monkeypatch.setattr(trust, '_is_windows', lambda: True)
    if 'real_bundle_resolver' not in request.fixturenames:
        monkeypatch.setattr(trust, '_windows_bundle_path', lambda: str(FIX / 'trusted-ca.pem'))
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)


@pytest.fixture
def real_bundle_resolver(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    (runtime / 'certifi').mkdir(parents=True)
    monkeypatch.setattr(sys, 'executable', str(runtime / 'python.exe'))
    monkeypatch.setattr(sys, 'path', [str(runtime)])
    monkeypatch.setattr(sys, 'path_importer_cache', dict(sys.path_importer_cache))
    # Record both absence and any existing module for reliable monkeypatch undo.
    monkeypatch.setitem(sys.modules, 'certifi', None)
    monkeypatch.delitem(sys.modules, 'certifi')
    return runtime


def _assert_system_only_bundle_missing():
    context = trust.get_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert trust.get_trust_diagnostic() == {'sources': ['windows-system', 'bundle-missing'],
                                          'status': 'supplemental bundle MISSING'}


def test_ssl_real_namespace_bundle_missing(real_bundle_resolver, monkeypatch):
    classifier = Mock(wraps=backend.classify_ytdlp_acquisition_error)
    monkeypatch.setattr(backend, 'classify_ytdlp_acquisition_error', classifier)
    try:
        _assert_system_only_bundle_missing()
    except Exception as error:
        classifier(error)
        raise
    module = sys.modules['certifi']
    assert module.__spec__.origin is None
    assert list(module.__path__) == [str(real_bundle_resolver / 'certifi')]
    classifier.assert_not_called()


@pytest.mark.parametrize('kind', ['missing', 'noncallable', 'attribute_error', 'import_error'])
def test_ssl_unusable_importable_bundle_is_missing(real_bundle_resolver, monkeypatch, kind):
    from types import SimpleNamespace
    if kind == 'import_error':
        monkeypatch.setitem(sys.modules, 'certifi', None)
    else:
        module = SimpleNamespace()
        if kind == 'noncallable':
            module.where = 'not a callable'
        elif kind == 'attribute_error':
            def where():
                raise AttributeError('private path in provider')
            module.where = where
        monkeypatch.setitem(sys.modules, 'certifi', module)
    _assert_system_only_bundle_missing()


@pytest.mark.parametrize('stage', ['import', 'where'])
def test_ssl_provider_runtime_error_is_tls_config(real_bundle_resolver, monkeypatch, stage):
    from types import SimpleNamespace
    private = str(real_bundle_resolver / 'private-provider-path')
    if stage == 'import':
        import builtins
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == 'certifi':
                raise RuntimeError(private)
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', importing)
    else:
        def where():
            raise RuntimeError(private)
        monkeypatch.setitem(sys.modules, 'certifi', SimpleNamespace(where=where))
    with pytest.raises(trust.SslTrustConfigError) as caught:
        trust.get_ssl_context()
    assert 'certifi-bundle' in str(caught.value)
    assert private not in str(caught.value) and str(real_bundle_resolver) not in str(caught.value)
    assert backend.classify_ytdlp_acquisition_error(caught.value) == 'tls_config'


@contextlib.contextmanager
def tls_server(leaf='trusted', redirect=None, plaintext=False, body=b'ok', tls_max=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(FIX / f'{leaf}.pem', FIX / 'server.key')
    if tls_max is not None:
        context.maximum_version = tls_max
    der = ssl.PEM_cert_to_DER_cert((FIX / f'{leaf}.pem').read_text())
    state = {'attempts': 0, 'completed': 0, 'selected': [], 'fingerprint': hashlib.sha256(der).hexdigest()}
    context.set_servername_callback(lambda *_: state['selected'].append(state['fingerprint']))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            state['attempts'] += 1
            connection.settimeout(5)
            try:
                with (connection if plaintext else context.wrap_socket(connection, server_side=True)) as stream:
                    state['completed'] += 1
                    stream.recv(4096)
                    response = (f'HTTP/1.1 302 Found\r\nLocation: {redirect}\r\n' if redirect
                                else 'HTTP/1.1 200 OK\r\n')
                    stream.sendall((response + f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body)
            except (ssl.SSLError, OSError):
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f'{"http" if plaintext else "https"}://127.0.0.1:{listener.getsockname()[1]}/', state
    finally:
        stop.set()
        listener.close()
        thread.join(6)
        assert not thread.is_alive(), 'TLS server did not terminate'


def opener(context):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      urllib.request.HTTPSHandler(context=context))


def test_ssl_windows_union_keeps_system_loading(monkeypatch):
    original = ssl.SSLContext.load_default_certs
    calls = []

    def load(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, 'load_default_certs', load)
    context = trust.get_ssl_context()
    assert calls == [True], 'Windows defaults must load before supplemental CAs'
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    expected = ssl.PEM_cert_to_DER_cert((FIX / 'trusted-ca.pem').read_text())
    assert expected in context.get_ca_certs(binary_form=True)
    diagnostic = trust.get_trust_diagnostic()
    assert diagnostic == {'sources': ['windows-system', 'certifi-bundle'], 'cert_count': 1,
                          'sha256': hashlib.sha256((FIX / 'trusted-ca.pem').read_bytes()).hexdigest()}
    assert str(FIX) not in json.dumps(diagnostic)


def test_ssl_trusted_cert_ok():
    context = trust.get_ssl_context()
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    with tls_server() as (url, state):
        port = int(url.split(':')[2].rstrip('/'))
        with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname='localhost') as stream:
                assert hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest() == state['fingerprint']
                stream.sendall(b'GET / HTTP/1.0\r\n\r\n')
                assert b'200 OK' in stream.recv(4096)
        assert state['completed'] == 1 and state['selected'] == [state['fingerprint']]


@pytest.mark.parametrize('leaf,code', [('untrusted', 20), ('wrong', 64), ('expired', 10)])
def test_ssl_rejects_untrusted_wrong_hostname_expired(leaf, code):
    with tls_server(leaf) as (url, state):
        with pytest.raises(urllib.error.URLError) as caught:
            opener(trust.get_ssl_context()).open(url, timeout=5)
        assert isinstance(caught.value.reason, ssl.SSLCertVerificationError)
        assert caught.value.reason.verify_code == code
        assert state['attempts'] == 1 and state['selected'] == [state['fingerprint']]


def test_ssl_missing_bundle_diagnostic(monkeypatch):
    monkeypatch.setattr(trust, '_windows_bundle_path', lambda: None)
    context = trust.get_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert trust.get_trust_diagnostic() == {'sources': ['windows-system', 'bundle-missing'],
                                          'status': 'supplemental bundle MISSING'}


@pytest.mark.parametrize('explicit', [False, True], ids=['bundle', 'SSL_CERT_FILE'])
@pytest.mark.parametrize('kind', ['missing', 'directory', 'binary', 'empty', 'malformed', 'zero_ca', 'unreadable', 'truncated'])
def test_ssl_invalid_ca_raises(tmp_path, monkeypatch, explicit, kind):
    path = tmp_path / 'private-ca-setting'
    if kind == 'directory':
        path.mkdir()
    elif kind != 'missing':
        path.write_bytes({'binary': b'\xff\x00', 'empty': b'', 'malformed': b'bad PEM',
                          'zero_ca': (FIX / 'trusted.pem').read_bytes(),
                          'truncated': (FIX / 'trusted-ca.pem').read_bytes() + b'-----BEGIN CERTIFICATE-----\nAA'}.get(kind, b'anything'))
    if kind == 'unreadable':
        import builtins
        original = builtins.open

        def deny(name, *args, **kwargs):
            if str(name) == str(path):
                raise PermissionError('private details')
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, 'open', deny)
    if explicit:
        monkeypatch.setenv('SSL_CERT_FILE', str(path))
    else:
        monkeypatch.setattr(trust, '_windows_bundle_path', lambda: str(path))
    with pytest.raises(trust.SslTrustConfigError) as caught:
        trust.get_ssl_context()
    assert ('SSL_CERT_FILE' if explicit else 'certifi-bundle') in str(caught.value)
    assert str(path) not in str(caught.value) and 'private details' not in str(caught.value)


def test_ssl_explicit_exclusive_precedence(monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE', str(FIX / 'untrusted-ca.pem'))
    monkeypatch.setattr(trust, '_windows_bundle_path', lambda: pytest.fail('bundle overrides explicit CA'))
    context = trust.get_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.cert_store_stats()['x509_ca'] == 1
    assert trust.get_trust_diagnostic()['sources'] == ['ssl_cert_file']
    with tls_server('trusted') as (url, state):
        with pytest.raises(urllib.error.URLError):
            opener(context).open(url, timeout=5)
        assert state['attempts'] == 1 and state['selected']
    with tls_server('untrusted') as (url, state):
        with opener(context).open(url, timeout=5) as response:
            assert response.read() == b'ok'
        assert state['completed'] == 1


def test_ssl_bytes_loaded_once(tmp_path, monkeypatch):
    import builtins
    import io
    path = tmp_path / 'ca.pem'
    original_bytes = (FIX / 'trusted-ca.pem').read_bytes()
    path.write_bytes(original_bytes)
    monkeypatch.setenv('SSL_CERT_FILE', str(path))
    original = builtins.open
    calls = []

    def read(name, *args, **kwargs):
        if str(name) == str(path):
            calls.append(True)
            with original(name, *args, **kwargs) as source:
                data = source.read()
            path.write_bytes((FIX / 'untrusted-ca.pem').read_bytes())
            return io.BytesIO(data)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', read)
    context = trust.get_ssl_context()
    assert calls == [True]
    assert trust.get_trust_diagnostic()['sha256'] == hashlib.sha256(original_bytes).hexdigest()
    assert ssl.PEM_cert_to_DER_cert(original_bytes.decode()) in context.get_ca_certs(binary_form=True)


@pytest.mark.parametrize('certifi,system', [('certifi.pem', 'system.pem'), (None, 'system.pem'), (None, None)])
def test_ssl_linux_unchanged(monkeypatch, certifi, system):
    monkeypatch.setattr(trust, '_is_windows', lambda: False)
    monkeypatch.setattr(trust, '_certifi_path', lambda: certifi)
    monkeypatch.setattr(trust, '_system_ca_path', lambda: system)
    factory = Mock()
    monkeypatch.setattr(ssl, 'create_default_context', factory)
    assert trust.get_ssl_context() is factory.return_value
    assert factory.call_args.kwargs == ({'cafile': certifi or system} if certifi or system else {})


def test_ssl_packaged_path_precedes_source_and_ignores_cwd(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(trust, '_windows_bundle_path', ORIGINAL_BUNDLE_PATH)
    executable = tmp_path / 'runtime/python.exe'
    bundle = executable.parent / 'certifi/cacert.pem'
    bundle.parent.mkdir(parents=True)
    bundle.write_text('fixture')
    monkeypatch.setattr(sys, 'executable', str(executable))
    source = tmp_path / 'source-certifi.pem'
    source.write_text('fixture')
    monkeypatch.setitem(sys.modules, 'certifi', SimpleNamespace(where=lambda: str(source)))
    monkeypatch.chdir(FIX)
    assert trust._windows_bundle_path() == str(bundle)
    bundle.unlink()
    assert trust._windows_bundle_path() == str(source)
    source.unlink()
    assert trust._windows_bundle_path() is None


@pytest.mark.parametrize('packaged', [True, False])
def test_ssl_unreadable_resolved_bundle_never_falls_back(tmp_path, monkeypatch, packaged):
    import builtins
    from types import SimpleNamespace
    monkeypatch.setattr(trust, '_windows_bundle_path', ORIGINAL_BUNDLE_PATH)
    executable = tmp_path / 'python.exe'
    bundled = str(tmp_path / 'certifi/cacert.pem')
    source = str(tmp_path / 'source.pem')
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setitem(sys.modules, 'certifi', SimpleNamespace(where=lambda: source))
    original_stat, original_open = trust.os.stat, builtins.open
    target = bundled if packaged else source

    def stat(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError('private path')
        return original_stat(path, *args, **kwargs)

    def read(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError('private path')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(trust.os, 'stat', stat)
    monkeypatch.setattr(builtins, 'open', read)
    assert trust._windows_bundle_path() == target
    with pytest.raises(trust.SslTrustConfigError, match='certifi-bundle: unreadable'):
        trust.get_ssl_context()


def test_ssl_source_bundle_never_resolves_relative_to_cwd(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(trust, '_windows_bundle_path', ORIGINAL_BUNDLE_PATH)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python.exe'))
    monkeypatch.setitem(sys.modules, 'certifi', SimpleNamespace(where=lambda: 'attacker.pem'))
    with pytest.raises(trust.SslTrustConfigError, match='CA path must be absolute'):
        trust.get_ssl_context()


def test_tls_redirect_rejects_untrusted_host(tmp_path, monkeypatch):
    contexts = []

    def context():
        value = trust.get_ssl_context()
        contexts.append(value)
        return value

    monkeypatch.setattr(backend, 'get_ssl_context', context)
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    with tls_server('untrusted') as (bad, rejected), tls_server(redirect=bad) as (good, trusted):
        with pytest.raises(urllib.error.URLError) as caught:
            backend._download_to(good, tmp_path / 'download')
        assert backend.classify_ytdlp_acquisition_error(caught.value) == 'tls_cert'
        assert trusted['completed'] == 1 and trusted['selected'] == [trusted['fingerprint']]
        assert rejected['attempts'] == 1 and rejected['selected'] == [rejected['fingerprint']]
        assert len(contexts) == 1
        assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname is True


@pytest.mark.parametrize('operation', ['metadata', 'download'])
@pytest.mark.parametrize('scheme', ['http', 'ftp'])
def test_tls_downgrade_rejected_before_plaintext_contact(tmp_path, monkeypatch, operation, scheme):
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    with tls_server(plaintext=True) as (plain, untouched), tls_server(redirect=plain.replace('http:', scheme + ':')) as (secure, served):
        monkeypatch.setattr(backend, 'YTDLP_API', secure)
        with pytest.raises(Exception) as caught:
            if operation == 'metadata':
                backend._ytdlp_fetch_latest()
            else:
                backend._download_to(secure, tmp_path / 'download')
        assert backend.classify_ytdlp_acquisition_error(caught.value) == 'insecure_redirect'
        assert untouched['attempts'] == 0
        assert served['attempts'] == served['completed'] == 1 and served['selected']


def test_tls_trusted_https_redirect_works(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    with tls_server() as (target, end), tls_server(redirect=target) as (source, start):
        backend._download_to(source, tmp_path / 'download')
        assert (tmp_path / 'download').read_bytes() == b'ok'
        assert start['attempts'] == start['completed'] == end['attempts'] == end['completed'] == 1
        assert start['selected'] and end['selected']


def test_tls_non_https_asset_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    with tls_server(plaintext=True) as (asset, untouched):
        body = json.dumps({'tag_name': 'test', 'assets': [
            {'name': 'yt-dlp.exe' if sys.platform == 'win32' else 'yt-dlp', 'browser_download_url': asset}]}).encode()
        with tls_server(body=body) as (api, served):
            monkeypatch.setattr(backend, 'YTDLP_API', api)
            with pytest.raises(Exception) as caught:
                backend._ytdlp_fetch_latest()
            assert backend.classify_ytdlp_acquisition_error(caught.value) == 'insecure_redirect'
            assert untouched['attempts'] == 0 and served['attempts'] == served['completed'] == 1


def test_tls_non_https_request_rejected_before_connect(tmp_path):
    with tls_server(plaintext=True) as (url, untouched):
        with pytest.raises(Exception) as caught:
            backend._download_to(url, tmp_path / 'download')
        assert backend.classify_ytdlp_acquisition_error(caught.value) == 'insecure_redirect'
        assert untouched['attempts'] == 0


def test_tls_incompatible_handshake_classified(monkeypatch):
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {})
    context = trust.get_ssl_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    with tls_server(tls_max=ssl.TLSVersion.TLSv1_2) as (url, state):
        with pytest.raises(urllib.error.URLError) as caught:
            opener(context).open(url, timeout=5)
        assert type(caught.value.reason) is ssl.SSLError
        assert backend.classify_ytdlp_acquisition_error(caught.value) == 'tls_protocol'
        assert state['attempts'] == 1


@pytest.mark.parametrize('leaf', ['trusted', 'untrusted'])
def test_ssl_two_root_union_serves_both(monkeypatch, leaf):
    def system(self, *args, **kwargs):
        self.load_verify_locations(cafile=str(FIX / 'untrusted-ca.pem'))
    monkeypatch.setattr(ssl.SSLContext, 'load_default_certs', system)
    context = trust.get_ssl_context()
    with tls_server(leaf) as (url, served):
        with opener(context).open(url, timeout=5) as response:
            assert response.read() == b'ok'
        assert served['attempts'] == served['completed'] == 1 and served['selected'] == [served['fingerprint']]


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('armor', ['CERTIFICATE', 'X509 CERTIFICATE', 'TRUSTED CERTIFICATE'])
def test_ssl_non_ascii_pem_comments_accepted(tmp_path, monkeypatch, explicit, armor):
    pem = (FIX / 'trusted-ca.pem').read_bytes().replace(b'CERTIFICATE', armor.encode())
    data = '# Subject: Z\u00fcrich\n'.encode() + pem + b'\n# \xff comment\n'
    path = tmp_path / 'ca.pem'
    path.write_bytes(data)
    if explicit:
        monkeypatch.setenv('SSL_CERT_FILE', str(path))
    else:
        monkeypatch.setattr(trust, '_windows_bundle_path', lambda: str(path))
    context = trust.get_ssl_context()
    assert context.cert_store_stats()['x509_ca'] >= 1
    assert trust.get_trust_diagnostic()['sha256'] == hashlib.sha256(data).hexdigest()


def test_ssl_real_resolver_directory_candidate_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(trust, '_windows_bundle_path', ORIGINAL_BUNDLE_PATH)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python.exe'))
    path = tmp_path / 'certifi/cacert.pem'
    path.mkdir(parents=True)
    assert trust._windows_bundle_path() == str(path)
    with pytest.raises(trust.SslTrustConfigError):
        trust.get_ssl_context()


def test_ssl_trusted_auxiliary_constraints_fail_closed(tmp_path, monkeypatch):
    import base64
    der = ssl.PEM_cert_to_DER_cert((FIX / 'trusted-ca.pem').read_text())
    path = tmp_path / 'ca.pem'
    path.write_bytes(b'-----BEGIN TRUSTED CERTIFICATE-----\n' + base64.b64encode(der + b'\x30\x00') +
                     b'\n-----END TRUSTED CERTIFICATE-----\n')
    monkeypatch.setenv('SSL_CERT_FILE', str(path))
    with pytest.raises(trust.SslTrustConfigError):
        trust.get_ssl_context()
