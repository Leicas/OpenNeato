import base64
import gzip
import io
import os
import struct
import tarfile
import unittest

try:
    from .stage_payload import build_payload, make_archive, validate_archive, sha, UPLOAD_LIMIT
except ImportError:
    from stage_payload import build_payload, make_archive, validate_archive, sha, UPLOAD_LIMIT


def candidate(payload=b"MAPS" + bytes(7932)):
    section = struct.pack("<4sII", b"TCES", len(payload), 0) + payload
    return b"TSAM" + bytes(4) + struct.pack("<I", 12 + len(section)) + section


class StagePayloadTests(unittest.TestCase):
    def test_roundtrip_archive_and_hashes(self):
        data = candidate()
        payload, report = build_payload(data)
        encoded = payload.split(b"<<'OPENNEATO_BASE64_END'\n", 1)[1].split(b"OPENNEATO_BASE64_END\n", 1)[0]
        archive = base64.b64decode(encoded)
        validate_archive(archive, data)
        self.assertEqual(sha(archive), report["archive_sha256"])
        self.assertEqual(sha(payload), report["payload_sha256"])
        self.assertLessEqual(len(payload), UPLOAD_LIMIT)
        self.assertTrue(payload.startswith(b"# OPENNEATO_MAINTENANCE_V1\n"))
        self.assertNotIn(b"/emmc", payload)
        self.assertNotIn(b"CopyDumps", payload)
        self.assertNotIn(b"qdbc", payload)
        self.assertIn(b"/bin/tar xOzf", payload)

    def test_deterministic(self):
        self.assertEqual(build_payload(candidate()), build_payload(candidate()))

    def test_malformed_container(self):
        for data in (b"invalid", candidate()[:-1], b"XXXX" + candidate()[4:]):
            with self.assertRaises(ValueError):
                build_payload(data)

    def test_incompressible_payload_exceeds_bridge_limit(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            build_payload(candidate(b"MAPS" + os.urandom(65536)))

    def test_archive_rejects_paths_links_and_extra_members(self):
        data = candidate()
        for names, link in ((["../permap.bin"], False), (["/permap.bin"], False),
                            (["permap.bin", "extra"], False), (["permap.bin"], True)):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name in names:
                    info = tarfile.TarInfo(name)
                    if link:
                        info.type, info.linkname = tarfile.SYMTYPE, "/etc/hosts"
                        archive.addfile(info)
                    else:
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
            with self.assertRaises(ValueError):
                validate_archive(gzip.compress(stream.getvalue()), data)

    def test_archive_rejects_changed_bytes(self):
        with self.assertRaises(ValueError):
            validate_archive(make_archive(candidate()), candidate(b"MAPSdifferent"))


if __name__ == "__main__":
    unittest.main()
