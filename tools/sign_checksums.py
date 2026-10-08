#!/usr/bin/env python3
"""Sign a release's SHA256SUMS.txt with the TrXi-Ctrl / trixie-gateway
release key (Ed25519), producing SHA256SUMS.txt.sig (raw 64-byte signature).
ISO 27001 gap G11: users can check a download came from us, not just that it
arrived intact.

Maintainer tool. The private key lives only on the release box:
  ~/.chameleon/trxi/release_signing_ed25519.key   (mode 600, never committed)
The public key is published at
  https://chameleon-ai-agent-ltd.net/.well-known/trxi-release.pub

  python3 tools/sign_checksums.py SHA256SUMS.txt         # sign
  python3 tools/sign_checksums.py --new-key               # first time only

Anyone can verify with OpenSSL 3:
  openssl pkeyutl -verify -pubin -inkey trxi-release.pub -rawin \\
      -in SHA256SUMS.txt -sigfile SHA256SUMS.txt.sig
  sha256sum -c SHA256SUMS.txt
"""
import os
import pathlib
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY = pathlib.Path.home() / ".chameleon" / "trxi" / "release_signing_ed25519.key"


def new_key() -> None:
    if KEY.exists():
        sys.exit(f"{KEY} already exists; refusing to overwrite a release key")
    KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    print(key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode(), end="")


def sign(path: str) -> None:
    key = serialization.load_pem_private_key(KEY.read_bytes(), password=None)
    data = pathlib.Path(path).read_bytes()
    sig = key.sign(data)
    key.public_key().verify(sig, data)          # never publish a bad signature
    pathlib.Path(path + ".sig").write_bytes(sig)
    print(f"wrote {path}.sig")


if __name__ == "__main__":
    if sys.argv[1:] == ["--new-key"]:
        new_key()
    elif len(sys.argv) == 2:
        sign(sys.argv[1])
    else:
        sys.exit(__doc__)
