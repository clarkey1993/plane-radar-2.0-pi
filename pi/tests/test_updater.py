import tarfile
import unittest

from plane_radar.updater import _validate_member, render_update_frame, version_tuple


class UpdaterTests(unittest.TestCase):
    def test_pi_version_comparison(self):
        self.assertGreater(version_tuple("pi-v2.1.0"), version_tuple("2.0.9"))

    def test_archive_traversal_is_rejected(self):
        member = tarfile.TarInfo("../../etc/passwd")
        with self.assertRaises(ValueError):
            _validate_member(member)

    def test_update_screen_renders_native_progress(self):
        image = render_update_frame(50, "Downloading software package", 3, "2.2.1")
        self.assertEqual(image.size, (320, 480))
        # The halfway point of the filled bar is green, while the far end is
        # still the dark unfilled track.
        self.assertGreater(image.getpixel((90, 324))[1], 150)
        self.assertLess(image.getpixel((260, 324))[1], 80)
