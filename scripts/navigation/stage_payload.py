"""Build an offline maintenance payload that stages a candidate in RAM only.

Container framing is checked, but candidate geometry and robot compatibility are
NOT established. No network, robot invocation, persistent install or apply exists.
"""

import argparse
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

try:
    from .permap import MAX_BYTES, parse
except ImportError:
    from permap import MAX_BYTES, parse

UPLOAD_LIMIT = 65536
MEMBER = "permap.bin"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_archive(data):
    """Return deterministic gzip/tar containing one fixed regular-file member."""
    if parse(data).serialize() != data:
        raise ValueError("Container does not reconstruct exactly")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo(MEMBER)
        info.mode, info.size, info.mtime = 0o600, len(data), 0
        archive.addfile(info, io.BytesIO(data))
    return gzip.compress(stream.getvalue(), compresslevel=9, mtime=0)


def validate_archive(archive, expected_data):
    """Reject extra members, aliases, links and changed contents before encoding."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        members = source.getmembers()
        if len(members) != 1 or members[0].name != MEMBER or not members[0].isreg():
            raise ValueError("Expected exactly one regular member named permap.bin")
        if members[0].size != len(expected_data):
            raise ValueError("Archive member size mismatch")
        if source.extractfile(members[0]).read() != expected_data:
            raise ValueError("Archive member content mismatch")


def build_payload(data):
    archive = make_archive(data)
    validate_archive(archive, data)
    encoded = base64.encodebytes(archive).decode("ascii")
    digest, archive_digest = sha(data), sha(archive)
    # All paths and shell substitutions derive from constants or hex digests.
    shell = f'''# OPENNEATO_MAINTENANCE_V1
# STAGING ONLY: scientifically unvalidated candidate; NOT a robot map install.
set -eu
umask 077
sleep 3
for cmd in openssl awk wc mkdir mv cat; do command -v "$cmd" >/dev/null; done
[ -x /bin/tar ]
STAGE=/tmp/openneato-stage-{digest[:16]}
# Refuse an existing directory instead of reusing or deleting unknown data.
mkdir "$STAGE"
cat > "$STAGE/input.b64" <<'OPENNEATO_BASE64_END'
{encoded}OPENNEATO_BASE64_END
openssl base64 -d -in "$STAGE/input.b64" -out "$STAGE/input.tar.gz"
[ "$(openssl dgst -sha256 "$STAGE/input.tar.gz" | awk '{{print $NF}}')" = '{archive_digest}' ]
/bin/tar xOzf "$STAGE/input.tar.gz" permap.bin > "$STAGE/permap.bin.part"
[ "$(wc -c < "$STAGE/permap.bin.part" | awk '{{print $1}}')" = '{len(data)}' ]
[ "$(openssl dgst -sha256 "$STAGE/permap.bin.part" | awk '{{print $NF}}')" = '{digest}' ]
mv "$STAGE/permap.bin.part" "$STAGE/permap.bin"
printf '%s\\n' '[[ram-staging-verified-not-installed]]' '{digest}'
'''.encode("ascii")
    if len(shell) > UPLOAD_LIMIT:
        raise ValueError(f"Final upload payload {len(shell)} exceeds {UPLOAD_LIMIT} bytes")
    return shell, {
        "staging_only": True, "scientifically_validated": False,
        "robot_compatibility_validated": False, "executed": False,
        "input_bytes": len(data), "input_sha256": digest,
        "archive_bytes": len(archive), "archive_sha256": archive_digest,
        "payload_bytes": len(shell), "payload_sha256": sha(shell),
        "archive_members": [MEMBER], "destination": f"/tmp/openneato-stage-{digest[:16]}/permap.bin",
        "limitations": ["QNX openssl base64 syntax not runtime tested",
                        "Staging does not load, install or validate a native map",
                        "No persistent write, restart or robot command is implemented"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    with args.input.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    payload, manifest = build_payload(data)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "stage-only.sh").write_bytes(payload)
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
