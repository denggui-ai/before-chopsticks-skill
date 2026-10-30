"""Synthetic-image checks for the P1 local edit boundary; no model calls."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image, ImageCms


SCRIPTS = Path(__file__).resolve().parents[2] / "runtime" / "before-chopsticks" / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    local_edits = importlib.import_module("bc_core.local_edits")
except ModuleNotFoundError as exc:
    if exc.name not in {"bc_core", "bc_core.local_edits"}:
        raise
    local_edits = None


class LocalEditsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(local_edits, "P1 local_edits module has not been implemented")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.master = self.root / "master.png"
        self.output = self.root / "render.png"

    def make_master(self, pixels=None, size=(3, 2), mode="RGB", **save_options):
        image = Image.new(mode, size)
        if pixels is None:
            pixels = [(40, 60, 80)] * (size[0] * size[1])
        image.putdata(pixels)
        image.save(self.master, **save_options)
        return self.master.read_bytes()

    def op(self, kind, params, **fields):
        return {"id": "op-1", "type": kind, "params": params, **fields}

    def render(self, operations, **kwargs):
        return local_edits.render_pipeline(self.master, operations, self.output, **kwargs)

    def pixels(self, path=None):
        with Image.open(path or self.output) as image:
            image.load()
            return list(image.get_flattened_data())

    def mask_source(self, geometry=None):
        return {
            "master_sha256": hashlib.sha256(self.master.read_bytes()).hexdigest(),
            "geometry": geometry or [],
        }

    def test_inspect_reads_metadata_without_modifying_original(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        original = self.make_master([(20, 30, 40, 50)] * 6, mode="RGBA", icc_profile=profile)
        info = local_edits.inspect_image(self.master)
        self.assertEqual(info, {"width": 3, "height": 2, "format": "PNG", "mode": "RGBA",
                                "has_alpha": True, "icc_profile_present": True})
        self.assertEqual(original, self.master.read_bytes())
        json.dumps(info, allow_nan=False)

    def test_inspect_rejects_truncated_or_unreadable_images(self):
        original = self.make_master(size=(20, 20), pixels=[(40, 50, 60)] * 400)
        self.master.write_bytes(original[:45])
        with self.assertRaises(ValueError):
            local_edits.inspect_image(self.master)
        with self.assertRaises(ValueError):
            local_edits.inspect_image(self.root / "missing.png")

    def test_inspect_and_render_share_display_oriented_coordinates(self):
        exif = Image.Exif()
        exif[274] = 6
        self.make_master(exif=exif)
        info = local_edits.inspect_image(self.master)
        self.assertEqual((info["width"], info["height"]), (2, 3))
        result = self.render([self.op("crop", {"box": [0, 1, 2, 3]})])
        self.assertEqual(result["operations"][0]["input_frame"], {"width": 2, "height": 3})
        self.assertEqual((result["media_info"]["width"], result["media_info"]["height"]), (2, 2))

    def test_basic_tone_operations_produce_expected_pixels(self):
        cases = [
            ("brightness", {"factor": 2}, [(80, 120, 160)]),
            ("contrast", {"factor": 0}, [(56, 56, 56)]),
            ("saturation", {"factor": 0}, [(56, 56, 56)]),
            ("warmth", {"amount": 1}, [(60, 67, 44)]),
        ]
        for kind, params, expected in cases:
            with self.subTest(kind=kind):
                self.make_master(size=(1, 1))
                self.output = self.root / f"{kind}.png"
                self.render([self.op(kind, params)])
                self.assertEqual(self.pixels(), expected)

    def test_pipeline_order_is_preserved_and_frames_are_normalized(self):
        original = self.make_master([(0, 0, 0), (100, 100, 100)], size=(2, 1))
        operations = [self.op("crop", {"box": [1, 0, 2, 1]}),
                      self.op("contrast", {"factor": 2}, id="op-2")]
        before = copy.deepcopy(operations)
        result = self.render(operations)
        self.assertEqual(self.pixels(), [(100, 100, 100)])
        self.assertEqual([op["id"] for op in result["operations"]], ["op-1", "op-2"])
        self.assertEqual(result["operations"][0]["input_frame"], {"width": 2, "height": 1})
        self.assertEqual(result["operations"][0]["output_frame"], {"width": 1, "height": 1})
        self.assertEqual(result["operations"][1]["input_frame"], {"width": 1, "height": 1})
        self.assertTrue(all(op["engine_version"] for op in result["operations"]))
        self.assertEqual(operations, before)
        self.assertEqual(original, self.master.read_bytes())
        self.assertEqual(set(result), {"media_info", "operations", "warnings"})
        json.dumps(result, allow_nan=False)
        self.output = self.root / "reverse.png"
        self.render([self.op("contrast", {"factor": 2}),
                     self.op("crop", {"box": [1, 0, 2, 1]}, id="op-2")])
        self.assertEqual(self.pixels(), [(150, 150, 150)])

    def test_invalid_frames_parameters_or_engine_are_rejected_before_output(self):
        self.make_master()
        bad_ops = [
            self.op("brightness", {"factor": 1}, input_frame={"width": 2, "height": 3}),
            self.op("crop", {"box": [0, 0, 2, 2]}, output_frame={"width": 3, "height": 2}),
            self.op("brightness", {"factor": 1}, input_frame={"width": True, "height": 2}),
            self.op("brightness", {"factor": 1}, engine_version="unknown-engine"),
            self.op("brightness", {"factor": True}),
            self.op("contrast", {"factor": float("nan")}),
            self.op("saturation", {"factor": float("inf")}),
            self.op("brightness", {"factor": 5}),
            self.op("brightness", {"factor": 10 ** 400}),
            self.op("warmth", {"amount": -2}),
            self.op("brightness", {"factor": 1, "subject": "guess"}),
            self.op("crop", {"box": [0, 0, 4, 2]}),
            self.op("crop", {"box": [0, 0, 0, 2]}),
            self.op("crop", {"box": [0, 0, 2.5, 2]}),
            self.op("crop", {"box": [False, 0, 2, 2]}),
            self.op("rotate", {"angle": 90}),
            self.op("brightness", {}),
        ]
        for operation in bad_ops:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    self.render([operation])
                self.assertFalse(self.output.exists())

    def test_alpha_and_icc_survive_tone_crop_and_saved_file_reload(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        self.make_master([(40, 60, 80, alpha) for alpha in [0, 1, 127, 200, 254, 255]],
                         mode="RGBA", icc_profile=profile)
        result = self.render([self.op("brightness", {"factor": 2}),
                              self.op("crop", {"box": [1, 0, 3, 2]}, id="crop")])
        with Image.open(self.output) as image:
            self.assertEqual(list(image.getchannel("A").get_flattened_data()), [1, 127, 254, 255])
            self.assertEqual(image.info.get("icc_profile"), profile)
        self.assertTrue(result["media_info"]["has_alpha"])
        self.assertTrue(result["media_info"]["icc_profile_present"])

    def test_local_brightness_only_changes_explicit_mask_pixels(self):
        original = self.make_master([(40, 60, 80, 127)] * 3, size=(3, 1), mode="RGBA")
        mask_path = self.root / "mask.png"
        mask = Image.new("L", (3, 1))
        mask.putdata([255, 0, 128])
        mask.save(mask_path)
        mask_bytes = mask_path.read_bytes()
        operation = self.op("brightness", {"factor": 2, "mask_source": self.mask_source()},
                            mask_asset_id="mask-1")
        result = self.render([operation], mask_paths={"mask-1": mask_path})
        self.assertEqual(self.pixels(), [(80, 120, 160, 127), (40, 60, 80, 127), (60, 90, 120, 127)])
        self.assertEqual(mask_path.read_bytes(), mask_bytes)
        self.assertEqual(self.master.read_bytes(), original)
        self.assertNotIn("protected_pixels_check", result)

    def test_crop_local_mask_requires_matching_master_and_geometry(self):
        self.make_master()
        mask_path = self.root / "mask.png"
        Image.new("L", (2, 2), 255).save(mask_path)
        crop = self.op("crop", {"box": [1, 0, 3, 2]})
        expected_geometry = [{"type": "crop", "box": [1, 0, 3, 2]}]
        good = self.op("brightness", {"factor": 2, "mask_source": self.mask_source(expected_geometry)},
                       id="local", mask_asset_id="mask-1")
        bad_sources = [None, self.mask_source(),
                       {"master_sha256": "0" * 64, "geometry": expected_geometry},
                       self.mask_source([{"type": "crop", "box": [0, 0, 2, 2]}])]
        for source in bad_sources:
            with self.subTest(source=source):
                invalid = copy.deepcopy(good)
                if source is None:
                    invalid["params"].pop("mask_source")
                else:
                    invalid["params"]["mask_source"] = source
                with self.assertRaises(ValueError):
                    self.render([crop, invalid], mask_paths={"mask-1": mask_path})
                self.assertFalse(self.output.exists())
        self.render([crop, good], mask_paths={"mask-1": mask_path})
        self.assertEqual(self.pixels(), [(80, 120, 160)] * 4)

    def test_local_mask_dimension_mode_asset_and_operation_are_validated(self):
        self.make_master()
        mask_path = self.root / "mask.png"
        good_params = {"factor": 2, "mask_source": self.mask_source()}
        operation = self.op("brightness", good_params, mask_asset_id="mask-1")
        with self.assertRaises(ValueError):
            self.render([operation])
        for mode, size in [("L", (2, 3)), ("RGB", (3, 2))]:
            with self.subTest(mode=mode, size=size):
                Image.new(mode, size).save(mask_path)
                with self.assertRaises(ValueError):
                    self.render([operation], mask_paths={"mask-1": mask_path})
        Image.new("L", (3, 2), 255).save(mask_path)
        with self.assertRaises(ValueError):
            self.render([self.op("contrast", good_params, mask_asset_id="mask-1")],
                        mask_paths={"mask-1": mask_path})
        self.assertFalse(self.output.exists())

    def test_output_cannot_overwrite_master_mask_or_any_existing_file(self):
        original = self.make_master()
        mask_path = self.root / "mask.png"
        Image.new("L", (3, 2), 255).save(mask_path)
        mask_bytes = mask_path.read_bytes()
        other = self.root / "existing.png"
        other.write_bytes(b"keep me")
        symlink = self.root / "alias.png"
        symlink.symlink_to(self.master)
        hardlink = self.root / "hardlink.png"
        os.link(self.master, hardlink)
        for output in [self.master, mask_path, other, symlink, hardlink]:
            with self.subTest(output=output.name):
                with self.assertRaises(ValueError):
                    local_edits.render_pipeline(self.master, [], output, mask_paths={"unused": mask_path})
        self.assertEqual(original, self.master.read_bytes())
        self.assertEqual(mask_bytes, mask_path.read_bytes())
        self.assertEqual(other.read_bytes(), b"keep me")

    def test_unsupported_color_modes_and_alpha_losing_exports_are_rejected(self):
        self.make_master([(1, 2, 3, 127)] * 6, mode="RGBA")
        self.output = self.root / "render.jpg"
        with self.assertRaises(ValueError):
            self.render([])
        self.assertFalse(self.output.exists())
        self.master = self.root / "cmyk.tif"
        Image.new("CMYK", (3, 2)).save(self.master)
        self.output = self.root / "render.png"
        with self.assertRaises(ValueError):
            self.render([])

    def test_lossy_export_has_explicit_warning_and_actual_media_metadata(self):
        self.make_master()
        self.output = self.root / "render.jpg"
        result = self.render([self.op("brightness", {"factor": 1.5})])
        self.assertEqual(result["media_info"]["format"], "JPEG")
        self.assertTrue(any("lossy" in warning.lower() for warning in result["warnings"]))
        self.assertEqual(local_edits.inspect_image(self.output), result["media_info"])

    def test_all_supported_encoders_preserve_icc_and_eligible_alpha(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for suffix, mode, pixels in [
            ("tiff", "RGBA", [(5, 25, 65, 0), (10, 30, 70, 128)]),
            ("webp", "RGBA", [(5, 25, 65, 0), (10, 30, 70, 128)]),
            ("jpg", "RGB", [(5, 25, 65), (10, 30, 70)]),
        ]:
            with self.subTest(suffix=suffix):
                self.make_master(pixels, size=(2, 1), mode=mode, icc_profile=profile)
                self.output = self.root / f"encoded.{suffix}"
                self.render([])
                with Image.open(self.output) as image:
                    self.assertEqual(image.info.get("icc_profile"), profile)
                    if mode == "RGBA":
                        self.assertEqual(list(image.get_flattened_data()), pixels)

    def test_high_depth_and_multi_frame_masters_are_rejected(self):
        self.master = self.root / "high.png"
        Image.new("I;16", (3, 2), 1000).save(self.master)
        with self.assertRaises(ValueError):
            self.render([])
        self.master = self.root / "animated.png"
        Image.new("RGB", (3, 2), "red").save(
            self.master, save_all=True, append_images=[Image.new("RGB", (3, 2), "green")])
        with self.assertRaises(ValueError):
            self.render([])
        self.assertFalse(self.output.exists())

    def test_replayed_normalized_pipeline_is_identical_and_independent(self):
        self.make_master()
        operations = [self.op("crop", {"box": [1, 0, 3, 2]}),
                      self.op("warmth", {"amount": .5}, id="warm")]
        first = self.render(operations)
        pixels = self.pixels()
        self.output = self.root / "replayed.png"
        replay = self.render(first["operations"])
        self.assertEqual(self.pixels(), pixels)
        self.assertEqual(replay["operations"], first["operations"])
        replay["operations"][0]["params"]["box"][0] = 0
        self.assertEqual(first["operations"][0]["params"]["box"], [1, 0, 3, 2])


if __name__ == "__main__":
    unittest.main()
