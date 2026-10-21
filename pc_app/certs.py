"""Self-signed TLS certificate so the phone browser treats the page as a secure
context (camera and motion sensors are only exposed to https:// or localhost)."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
from pathlib import Path


def primary_ip() -> str | None:
    try:  # interface of the default route; UDP connect sends no packets
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ips() -> list[str]:
    """All local IPv4 addresses, the default-route interface first."""
    ips = {"127.0.0.1"}
    first = primary_ip()
    if first:
        ips.add(first)
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    return sorted((i for i in ips if not i.startswith("169.254.")), key=lambda i: (i != first, i))


def ensure_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Create (or refresh when the LAN IPs changed) cert.pem / key.pem."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_p, key_p, meta_p = cert_dir / "cert.pem", cert_dir / "key.pem", cert_dir / "san.json"
    ips = lan_ips()
    if cert_p.exists() and key_p.exists() and meta_p.exists():
        if set(ips) <= set(json.loads(meta_p.read_text())):
            return cert_p, key_p

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone-perception-dev")])
    san = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
    san += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    meta_p.write_text(json.dumps(ips))
    return cert_p, key_p
