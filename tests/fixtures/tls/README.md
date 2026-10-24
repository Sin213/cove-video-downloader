These are synthetic, TEST-ONLY P-256 certificates and a shared server private
key. They have no production use or real identities. No production keys were
used. The two CA private keys exist only in `.tmp-slice3b/fixture-generation/`.

Regenerate from the repository root with the exact command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python tests/fixtures/tls/regenerate.py
```

`regenerate.py` records the complete OpenSSL argv for every operation and uses
`C:\Program Files\Git\usr\bin\openssl.exe`. It contains all exact regeneration
commands and extension settings. Random keys/serial signatures will differ.
Trusted and untrusted roots have critical CA:TRUE and keyCertSign/cRLSign;
leaves have CA:FALSE, digitalSignature, serverAuth, SKI, AKI, and SANs.
`wrong.pem` names wrong.invalid. `expired.pem` expired on 2001-01-01.
Other leaves are valid through 2045-01-01. Roots last 7300 days from generation.
The shared server key matches every leaf. Tests bind only 127.0.0.1 and verify
connection attempts and the certificate fingerprint served by successful TLS.
