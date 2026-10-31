"""R8.01: centred maximum crop boxes for the ratio presets documented in interaction.md."""

import importlib
from pathlib import Path
import re
import sys
import tempfile
import unittest

from PIL import Image

RUNTIME = Path(__file__).resolve().parents[2] / "runtime/before-chopsticks"
sys.path.insert(0, str(RUNTIME / "scripts"))
local_edits = importlib.import_module("bc_core.local_edits")

DOCUMENTED = [
    ((1, 1), [50, 0, 350, 300]),
    ((3, 4), [87, 0, 312, 300]),
    ((4, 5), [80, 0, 320, 300]),
    ((16, 9), [0, 37, 400, 262]),
    ((9, 16), [116, 0, 284, 300]),
]


def centred_box(width, height, a, b):
    if width * b > height * a:
        new_width = height * a // b
        left = (width - new_width) // 2
        return [left, 0, left + new_width, height]
    new_height = width * b // a
    top = (height - new_height) // 2
    return [0, top, width, top + new_height]


class RatioPresetTests(unittest.TestCase):
    def test_interaction_doc_lists_the_same_example_boxes(self):
        text = (RUNTIME / "references/core/interaction.md").read_text(encoding="utf-8")
        rows = re.findall(r"\|\s*(\d+):(\d+)\s*\|[^|\n]*\|\s*\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\s*\|", text)
        found = [((int(a), int(b)), [int(x) for x in box]) for a, b, *box in rows]
        self.assertEqual(found, DOCUMENTED)

    def test_documented_boxes_follow_the_formula(self):
        for (a, b), box in DOCUMENTED:
            self.assertEqual(centred_box(400, 300, a, b), box, (a, b))

    def test_each_preset_is_a_separate_crop_of_the_same_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.png"
            Image.new("RGB", (400, 300), (40, 60, 80)).save(master)
            for (a, b), box in DOCUMENTED:
                output = Path(tmp) / f"{a}x{b}.png"
                local_edits.render_pipeline(master, [{"id": "op-1", "type": "crop", "params": {"box": box}}], output)
                with Image.open(output) as image:
                    width, height = image.size
                self.assertEqual((width, height), (box[2] - box[0], box[3] - box[1]))
                self.assertLessEqual(abs(width / height - a / b), 1 / height, (a, b))


if __name__ == "__main__":
    unittest.main()
