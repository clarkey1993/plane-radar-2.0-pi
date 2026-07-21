import tarfile
import unittest

from plane_radar.updater import _validate_member, version_tuple


class UpdaterTests(unittest.TestCase):
    def test_pi_version_comparison(self):
        self.assertGreater(version_tuple("pi-v2.1.0"), version_tuple("2.0.9"))

    def test_archive_traversal_is_rejected(self):
        member = tarfile.TarInfo("../../etc/passwd")
        with self.assertRaises(ValueError):
            _validate_member(member)
