"""Manually regenerate TEST-ONLY EC credentials; all working files stay in scratch."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'tests/fixtures/tls'
WORK = ROOT / '.tmp-slice3b/fixture-generation'
WORK.mkdir(parents=True, exist_ok=True)
OPENSSL = r'C:\Program Files\Git\usr\bin\openssl.exe'
(WORK / 'empty.cnf').write_text('')
ENV = dict(os.environ, OPENSSL_CONF=str(WORK / 'empty.cnf'))


def run(*args):
    command = [OPENSSL, *map(str, args)]
    print(repr(command), flush=True)
    subprocess.run(command, env=ENV, check=True)


run('version')
run('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
    '-out', OUT / 'server.key')
for name in ('trusted', 'untrusted'):
    key, cert = WORK / f'{name}.key', OUT / f'{name}-ca.pem'
    run('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
        '-nodes', '-subj', f'/CN=Cove TEST ONLY {name} CA', '-days', '7300',
        '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
        '-addext', 'subjectKeyIdentifier=hash', '-keyout', key, '-out', cert)
    database = WORK / f'{name}.index'
    database.write_text('')
    serial = WORK / f'{name}.serial'
    serial.write_text('1000\n')
    config = WORK / f'{name}.cnf'
    config.write_text(f'''[ca]
default_ca=local
[local]
database={database.as_posix()}
serial={serial.as_posix()}
new_certs_dir={WORK.as_posix()}
certificate={cert.as_posix()}
private_key={key.as_posix()}
default_md=sha256
policy=policy
[policy]
commonName=supplied
''')
    for leaf in (('trusted', 'wrong', 'expired') if name == 'trusted' else ('untrusted',)):
        csr, ext = WORK / f'{leaf}.csr', WORK / f'{leaf}.ext'
        run('req', '-new', '-key', OUT / 'server.key', '-subj', f'/CN=Cove TEST ONLY {leaf}', '-out', csr)
        san = 'DNS:wrong.invalid' if leaf == 'wrong' else 'DNS:localhost,IP:127.0.0.1'
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                       'extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n'
                       f'authorityKeyIdentifier=keyid,issuer\nsubjectAltName={san}\n')
        run('ca', '-batch', '-notext', '-config', config, '-extfile', ext,
            '-startdate', '20000101000000Z' if leaf == 'expired' else '20200101000000Z',
            '-enddate', '20010101000000Z' if leaf == 'expired' else '20450101000000Z',
            '-in', csr, '-out', OUT / f'{leaf}.pem')
