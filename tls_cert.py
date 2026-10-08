"""The gateway's own TLS certificate for home-network (LAN) connections
(ISO 27001 gap G5). Self-signed and long-lived on purpose: the apps don't
trust it through a CA, they pin its SHA-256 fingerprint, which travels in
the pairing QR (or is shown on /admin to compare when pairing by code).
Tailscale connections stay on plain HTTP because Tailscale already encrypts
them end to end.
"""
import datetime
import hashlib
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TLS_DIR = Path.home() / ".config" / "trixie-gateway" / "tls"


def ensure(tls_dir: Path = TLS_DIR) -> tuple[Path, Path]:
    """(cert.pem, key.pem), created once (key 0600)."""
    cert_p, key_p = tls_dir / "cert.pem", tls_dir / "key.pem"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    tls_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "trixie-gateway")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    fd = os.open(key_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


def fingerprint(cert_p: Path) -> str:
    """SHA-256 of the certificate (DER), lower-case hex: what the apps pin."""
    der = ssl.PEM_cert_to_DER_cert(cert_p.read_text())
    return hashlib.sha256(der).hexdigest()


def server_context(tls_dir: Path = TLS_DIR) -> tuple[ssl.SSLContext, str]:
    cert_p, key_p = ensure(tls_dir)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_p, key_p)
    return ctx, fingerprint(cert_p)


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    ctx, fp = server_context(d)
    assert len(fp) == 64 and oct((d / "key.pem").stat().st_mode)[-3:] == "600"
    assert server_context(d)[1] == fp            # stable across restarts
    print("tls_cert selftest OK")
