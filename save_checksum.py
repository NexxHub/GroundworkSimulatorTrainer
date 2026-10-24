"""Groundwork Simulator save-envelope integrity algorithm, verified 2026-10-24.

Derived from the installed executable's validator (VA 0x1466f5130) and digest
routine (VA 0x1466ede50), then checked against the current and previous saves.
No decompression is required. Metadata, magic, version and baseline are not
part of this digest. The game checks magic/version/baseline independently.
"""
import hashlib
import struct


def compute_checksum(envelope):
    """Return the 32-byte SHA-256 digest expected by the save envelope."""
    compressed = envelope['compressed_world_payload']
    h = hashlib.sha256(bytes(compressed))
    # Rust BTreeMap<String, Vec<u8>> iteration is byte-lexicographic UTF-8 order.
    for key, payload in sorted(envelope.get('extension_payloads', {}).items(),
                               key=lambda pair: pair[0].encode('utf-8')):
        name = key.encode('utf-8')
        content = bytes(payload)
        h.update(struct.pack('<Q', len(name)))
        h.update(name)
        h.update(struct.pack('<Q', len(content)))
        h.update(content)
    return h.digest()
