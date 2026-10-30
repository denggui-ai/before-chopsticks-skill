"""Deterministic P1 image edits; callers own Asset/Version selection and storage.

Operations are ordered dicts with a unique id, type and params. Brightness,
contrast and saturation accept ``factor`` in [0, 4] (1 is unchanged); warmth
accepts ``amount`` in [-1, 1] (0 is unchanged). Warmth scales R/G/B by
1 + .5*amount, 1 + .12*amount, and 1 - .45*amount. Crop accepts integer pixel
``box: [left, top, right, bottom]`` within the current image. Frames use only
``{width, height}``, in EXIF-corrected display coordinates. Supplied frames or
engine versions must match; absent values are filled in the returned copy.

Local brightness additionally requires mask_asset_id, its path in mask_paths,
and params.mask_source = {master_sha256, geometry}. The SHA-256 identifies the
actual master bytes; geometry is the complete preceding crop sequence, e.g.
[{"type": "crop", "box": [1, 0, 3, 2]}]. Masks must be explicit 1/L images in
that frame: white applies the edit, black preserves the pixel, grey blends.
The caller must supply trustworthy mask provenance; a declaration only binds
coordinates, it does not establish a semantic food region. The module never
guesses a region, transforms an unrelated mask, or reports a semantic check.

Rendering supports single-frame 8-bit RGB/RGBA masters and PNG/TIFF/JPEG/WebP
outputs. EXIF orientation is applied once and removed from the rendered image.
The ICC bytes and alpha are preserved and checked after reopening the saved
file. JPEG is refused for alpha images and otherwise emits a lossy warning.
WebP is explicitly lossless. Unsupported inputs/operations raise ValueError.
Every output must be a new path; original images and masks remain untouched.
"""
from copy import deepcopy
import hashlib
import io
import math
import os
from pathlib import Path

import PIL
from PIL import Image, ImageEnhance, ImageOps


ENGINE_VERSION = f"before-chopsticks-local/1;pillow/{PIL.__version__}"
_FORMATS = {".png": "PNG", ".tif": "TIFF", ".tiff": "TIFF",
            ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
_TONE = {"brightness": ImageEnhance.Brightness,
         "contrast": ImageEnhance.Contrast, "saturation": ImageEnhance.Color}
_FIELDS = {"id", "type", "params", "input_frame", "output_frame", "mask_asset_id", "engine_version"}


def _read_image(path):
    """Decode a single byte snapshot so provenance and pixels cannot diverge."""
    try:
        data = Path(path).expanduser().read_bytes()
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image_format = source.format
            frames = getattr(source, "n_frames", 1)
            high_depth = source.mode in {"I", "F"} or source.mode.startswith("I;")
            if image_format == "PNG" and len(data) > 24:
                high_depth = high_depth or data[24] > 8
            if image_format == "TIFF":
                bits = source.tag_v2.get(258, (8,))
                if isinstance(bits, int):
                    bits = (bits,)
                high_depth = high_depth or any(bit > 8 for bit in bits)
            image = ImageOps.exif_transpose(source)
            image.load()
        return image, image_format, frames, high_depth, data
    except (OSError, ValueError, TypeError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Image is not readable: {path}: {exc}") from exc


def _media_info(image, image_format):
    return {"width": image.width, "height": image.height,
            "format": image_format, "mode": image.mode,
            "has_alpha": "A" in image.getbands() or "transparency" in image.info,
            "icc_profile_present": bool(image.info.get("icc_profile"))}


def inspect_image(path):
    """Read metadata and pixels without modifying the image at path."""
    image, image_format, _, _, _ = _read_image(path)
    return _media_info(image, image_format)


def _frame(size):
    return {"width": size[0], "height": size[1]}


def _check_frame(operation, name, size):
    expected = _frame(size)
    if name in operation:
        supplied = operation[name]
        if (not isinstance(supplied, dict) or set(supplied) != {"width", "height"}
                or any(type(supplied[key]) is not int for key in expected)
                or supplied != expected):
            raise ValueError(f"{name} does not match the actual pipeline frame {expected}")
    return expected


def _number(value, name, low, high):
    if (type(value) not in {int, float} or not low <= value <= high
            or not math.isfinite(value)):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    return float(value)


def _box(value, size):
    if (not isinstance(value, list) or len(value) != 4
            or any(type(item) is not int for item in value)):
        raise ValueError("crop box must contain four integer pixel coordinates")
    left, top, right, bottom = value
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError("crop box must be nonempty and within the current frame")
    return list(value)


def _mask(operation, mask_paths, size, master_sha256, geometry):
    asset_id = operation["mask_asset_id"]
    if not isinstance(asset_id, str) or not asset_id or asset_id not in mask_paths:
        raise ValueError("local brightness requires an explicitly mapped mask asset")
    provenance = operation["params"].get("mask_source")
    expected = {"master_sha256": master_sha256, "geometry": geometry}
    if provenance != expected:
        raise ValueError("mask_source must match this master and preceding geometry")
    # Equality alone would accept bool coordinates as integers.
    if (not isinstance(provenance, dict) or not isinstance(provenance.get("geometry"), list)
            or any(not isinstance(item, dict) or set(item) != {"type", "box"}
                   or not isinstance(item["box"], list)
                   or any(type(value) is not int for value in item["box"])
                   for item in provenance["geometry"])):
        raise ValueError("mask_source geometry must contain explicit integer crop coordinates")
    image, _, frames, high_depth, _ = _read_image(mask_paths[asset_id])
    if frames != 1 or high_depth or image.mode not in {"1", "L"}:
        raise ValueError("mask must be a single-frame 1-bit or 8-bit grayscale image")
    if image.size != size:
        raise ValueError("mask dimensions do not match its declared coordinate frame")
    return image.convert("L")


def _warmth(image, amount):
    gains = (1 + amount * .5, 1 + amount * .12, 1 - amount * .45)
    channels = [channel.point([min(255, max(0, round(value * gain))) for value in range(256)])
                for channel, gain in zip(image.split(), gains)]
    return Image.merge("RGB", channels)


def _save_verified(image, output, image_format, icc):
    options = {"icc_profile": icc} if icc else {}
    if image_format == "JPEG":
        options.update(quality=95, subsampling=0)
    elif image_format == "WEBP":
        options.update(lossless=True, exact=True)
    # Exclusive creation closes the preflight/create race and rejects symlinks.
    try:
        handle = output.open("xb")
    except OSError as exc:
        raise ValueError(f"Output must be a new writable file: {output}: {exc}") from exc
    try:
        with handle:
            image.save(handle, format=image_format, **options)
            handle.flush()
            os.fsync(handle.fileno())
        saved, saved_format, _, _, _ = _read_image(output)
        if saved.size != image.size or saved_format != image_format:
            raise ValueError("Saved image dimensions or format differ from the render")
        if saved.info.get("icc_profile") != icc:
            raise ValueError("Saved image did not preserve its ICC profile")
        if "A" in image.getbands():
            if "A" not in saved.getbands() or saved.getchannel("A").tobytes() != image.getchannel("A").tobytes():
                raise ValueError("Saved image did not preserve alpha")
        if image_format != "JPEG" and (saved.mode != image.mode or saved.tobytes() != image.tobytes()):
            raise ValueError("Lossless export changed rendered pixels")
        return _media_info(saved, saved_format)
    except Exception as exc:
        output.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Could not save and verify output: {exc}") from exc


def render_pipeline(master_path, operations, output_path, mask_paths=None):
    """Render in array order; return normalized operations and saved media info."""
    if not isinstance(operations, list):
        raise ValueError("operations must be an ordered list")
    if mask_paths is None:
        mask_paths = {}
    if not isinstance(mask_paths, dict):
        raise ValueError("mask_paths must map asset IDs to paths")
    master = Path(master_path).expanduser().resolve()
    output = Path(output_path).expanduser().absolute()
    protected = {master, *(Path(path).expanduser().resolve() for path in mask_paths.values())}
    if output.resolve() in protected or output.exists() or output.is_symlink():
        raise ValueError("Output cannot overwrite the master, a mask, or any existing file")
    image_format = _FORMATS.get(output.suffix.lower())
    if image_format is None:
        raise ValueError("Supported output extensions are PNG, TIFF, JPEG and WebP")
    image, _, frames, high_depth, master_bytes = _read_image(master)
    if frames != 1 or high_depth or image.mode not in {"RGB", "RGBA"}:
        raise ValueError("Rendering supports only single-frame 8-bit RGB/RGBA masters")
    icc = image.info.get("icc_profile")
    if "transparency" in image.info:
        image = image.convert("RGBA")
    if image_format == "JPEG" and "A" in image.getbands():
        raise ValueError("JPEG would discard alpha; use PNG, TIFF or WebP")
    master_sha256 = hashlib.sha256(master_bytes).hexdigest()
    normalized, geometry, seen_ids = [], [], set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) - _FIELDS:
            raise ValueError("Operation must contain only supported fields")
        op_id = operation.get("id")
        if not isinstance(op_id, str) or not op_id.strip() or op_id in seen_ids:
            raise ValueError("Every operation needs a unique, nonempty string id")
        seen_ids.add(op_id)
        kind, params = operation.get("type"), operation.get("params")
        if not isinstance(kind, str) or kind not in {*_TONE, "warmth", "crop"}:
            raise ValueError(f"Unsupported operation: {kind}")
        if not isinstance(params, dict):
            raise ValueError("Operation params must be an object")
        if "engine_version" in operation and operation["engine_version"] != ENGINE_VERSION:
            raise ValueError("Operation engine_version is unsupported")
        current = {"id": op_id, "type": kind, "params": deepcopy(params),
                   "input_frame": _check_frame(operation, "input_frame", image.size),
                   "engine_version": ENGINE_VERSION}
        has_mask = "mask_asset_id" in operation
        allowed = {"box"} if kind == "crop" else {"amount"} if kind == "warmth" else {"factor"}
        if has_mask:
            if kind != "brightness":
                raise ValueError("Only brightness supports a local mask")
            allowed.add("mask_source")
        if set(params) != allowed:
            raise ValueError(f"{kind} params must contain exactly {sorted(allowed)}")
        if kind == "crop":
            box = _box(params["box"], image.size)
            image = image.crop(box)
            geometry.append({"type": "crop", "box": box})
        else:
            mask = _mask(operation, mask_paths, image.size, master_sha256, geometry) if has_mask else None
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            rgb = image.convert("RGB")
            if kind == "warmth":
                amount = _number(params["amount"], "amount", -1, 1)
                current["params"]["amount"] = amount
                changed = _warmth(rgb, amount)
            else:
                factor = _number(params["factor"], "factor", 0, 4)
                current["params"]["factor"] = factor
                changed = _TONE[kind](rgb).enhance(factor)
            image = Image.composite(changed, rgb, mask) if mask is not None else changed
            if alpha is not None:
                image.putalpha(alpha)
            if has_mask:
                current["mask_asset_id"] = operation["mask_asset_id"]
        current["output_frame"] = _check_frame(operation, "output_frame", image.size)
        normalized.append(current)
    # Pixel transforms create fresh images; do not propagate stale EXIF or a
    # transparency color key. Only the verified ICC profile is explicitly saved.
    image.info.clear()
    info = _save_verified(image, output, image_format, icc)
    warnings = ["JPEG export is lossy; pixel equality is not claimed."] if image_format == "JPEG" else []
    return {"media_info": info, "operations": normalized, "warnings": warnings}
