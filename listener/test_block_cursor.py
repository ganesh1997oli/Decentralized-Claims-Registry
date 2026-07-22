import tempfile
import unittest
from pathlib import Path

from listener.block_cursor import BlockCursor, BlockCursorError


class BlockCursorTests(unittest.TestCase):
    def test_saves_and_reloads_a_matching_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor.json"
            cursor = BlockCursor(path, 11155111, "0xabc")

            self.assertEqual(cursor.load(default=90), 90)
            cursor.save(100)

            self.assertEqual(cursor.load(default=90), 100)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_rejects_a_checkpoint_for_another_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor.json"
            BlockCursor(path, 11155111, "0xabc").save(100)

            with self.assertRaises(BlockCursorError):
                BlockCursor(path, 11155111, "0xdef").load(default=90)


if __name__ == "__main__":
    unittest.main()
