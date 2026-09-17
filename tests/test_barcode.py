import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from barcode_reader import BarcodeReader, INPUT_EVENT, ScanDecoder, discover_devices


class BarcodeTests(unittest.TestCase):
    def test_verified_honeywell_sn(self):
        decoder = ScanDecoder()
        for code in [2,11,2,11,4,8,11,5]:
            self.assertIsNone(decoder.feed(1, code, 1))
            decoder.feed(1, code, 0)
        decoder.feed(1, 42, 1)
        decoder.feed(1, 18, 1)
        decoder.feed(1, 22, 1)
        decoder.feed(1, 42, 0)
        self.assertEqual(decoder.feed(1, 28, 1), "10103704EU")
        self.assertIsNone(decoder.feed(1, 28, 0))
        self.assertIsNone(decoder.feed(1, 28, 1))

    def test_repeats_and_release_do_not_duplicate(self):
        decoder = ScanDecoder()
        for value in [1,2,2,0]:
            decoder.feed(1, 2, value)
        self.assertEqual(decoder.feed(1,28,1), "1")

    def test_same_sn_can_be_decoded_in_consecutive_cycles(self):
        decoder = ScanDecoder()
        for _ in range(2):
            for code in [2, 3, 4, 5]:
                decoder.feed(1, code, 1)
                decoder.feed(1, code, 0)
            self.assertEqual(decoder.feed(1, 28, 1), "1234")
            decoder.feed(1, 28, 0)

    def test_reconnect_requests_hid_reopen(self):
        reader = BarcodeReader(Mock())
        reader.thread = Mock()
        reader.thread.is_alive.return_value = True
        reader.reconnect()
        self.assertTrue(reader.reconnect_event.is_set())

    def test_incomplete_timeout_rejects_suffix(self):
        decoder = ScanDecoder(timeout=1)
        decoder.feed(1,2,1,now=0)
        decoder.feed(1,3,1,now=2)
        self.assertIsNone(decoder.feed(1,28,1,now=2.1))
        decoder.feed(1,4,1,now=2.2)
        self.assertEqual(decoder.feed(1,28,1,now=2.3), "3")

    def test_unsupported_overlong_and_sync_drop_fail_closed(self):
        for issue in ("unsupported", "overflow", "dropped"):
            decoder = ScanDecoder(max_length=2)
            decoder.feed(1,2,1)
            if issue == "unsupported":
                decoder.feed(1,59,1)
            elif issue == "overflow":
                decoder.feed(1,2,1)
                decoder.feed(1,2,1)
            else:
                decoder.feed(0,3,0)
            self.assertIsNone(decoder.feed(1,28,1))

    def test_disconnect_reset_clears_shift_and_partial_sn(self):
        decoder = ScanDecoder()
        decoder.feed(1,42,1)
        decoder.feed(1,18,1)
        decoder.reset()
        decoder.feed(1,22,1)
        self.assertEqual(decoder.feed(1,28,1), "u")

    def test_native_event_roundtrip(self):
        packed = INPUT_EVENT.pack(1,2,1,28,1)
        self.assertEqual(INPUT_EVENT.unpack(packed), (1,2,1,28,1))

    def test_discovers_honeywell_ids_on_usb_parent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = root / "sys" / "event6"
            device = event / "device"
            (device / "capabilities").mkdir(parents=True)
            (device / "name").write_text("Honeywell Xenon 1900g\n", encoding="utf-8")
            (device / "capabilities" / "key").write_text("10000000\n", encoding="ascii")
            (event / "idVendor").write_text("0C2E\n", encoding="ascii")
            (event / "idProduct").write_text("0901\n", encoding="ascii")
            found = list(discover_devices(
                "0x0c2e", "901", sys_root=root / "sys", dev_root=root / "dev"
            ))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["path"], root / "dev" / "event6")
            self.assertEqual(found[0]["vendor_id"], "0c2e")
            self.assertEqual(found[0]["product_id"], "0901")


if __name__ == "__main__":
    unittest.main()
