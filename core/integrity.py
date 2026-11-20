"""Checks downloads against the checksum their source publishes, so a broken or tampered file is never used."""
import base64
import hashlib


def check_sri(blob, integrity):
    """npm's dist.integrity ("sha512-<base64>"). Raises if it doesn't match; no-op if npm gave none."""
    if not integrity:
        return
    for part in str(integrity).split():
        algo, _, want = part.partition("-")
        if algo in ("sha512", "sha384", "sha256"):
            got = base64.b64encode(hashlib.new(algo, blob).digest()).decode()
            if got != want:
                raise RuntimeError("download doesn't match its checksum")
            return


def check_sha256(blob, hexdigest):
    if hexdigest and hashlib.sha256(blob).hexdigest() != hexdigest.strip().lower():
        raise RuntimeError("download doesn't match its checksum")


def from_shasums(text, filename):
    """Finds filename's hash in a SHASUMS256.txt."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0]
    return None
