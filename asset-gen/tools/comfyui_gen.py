#!/usr/bin/env python3
"""Free local image and video generation via the self-hosted ComfyUI box.

Talks to a ComfyUI server reachable only on the remote host's
loopback, by opening a short-lived SSH local-forward to it. No API keys, no
per-generation charge — the box has 2x RTX 4090.

Subcommands:
  image   text2img or instruction edit (FLUX.2 Klein 4B production/pixel)
  video   text/image/first+last-frame to video with native audio (MiniMax H3)
  doctor  report which local model inventories and required ComfyUI nodes are complete

Output: JSON to stdout {"ok": true, "path": "...", "cost_cents": 0, "backend": "comfyui"}.
Progress goes to stderr — redirect it and read only on failure.

The paid cloud backends (Gemini/Grok/Tripo3D) live in asset_gen.py. Use this
when the asset is cheap to iterate on and exact prompt adherence isn't critical,
or to avoid spend. See comfyui.md for model licenses and the full capability map.
"""
import argparse
import json
import math
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Self-hosted ComfyUI box (sg-office, 2x RTX 4090). API is loopback-only on the
# remote, so we reach it through an SSH tunnel.
SSH_HOST = "root@101.78.126.6"
REMOTE_API_PORT = 8188

IMAGE_PROFILES = {
    "production": {
        "name": "flux2-klein-base-4b",
        "unet": ["flux-2-klein-base-4b-fp8.safetensors"],
        "clip": ["qwen_3_4b.safetensors"],
        "vae": ["flux2-vae.safetensors"],
        "default_size": 1024,
        "default_steps": 50,
        "default_guidance": 4.0,
        "supports_edit": True,
    },
    "pixel": {
        "name": "flux2-klein-4b-pixel-lora",
        "unet": ["flux-2-klein-4b-fp8.safetensors"],
        "clip": ["qwen_3_4b.safetensors"],
        "vae": ["flux2-vae.safetensors"],
        "lora": ["pytorch_lora_weights.comfyui.safetensors"],
        "default_size": 512,
        "default_steps": 4,
        "default_guidance": 1.0,
        "supports_edit": False,
    },
}

H3_PROFILE = {
    "unet": ["minimax_h3_fl2va_pruned_int8_convrot.safetensors"],
    "clip": ["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"],
    "video_vae": ["minimax_h3_video_vae_fp16.safetensors"],
    "audio_vae": ["minimax_h3_audio_vae_fp32.safetensors"],
}
H3_MODEL = "minimax-h3-base-fl2va"

IMAGE_COMMON_NODES = {"UNETLoader", "CLIPLoader", "VAELoader",
                      "CLIPTextEncode", "EmptyFlux2LatentImage",
                      "VAEDecode", "SaveImage"}
PRODUCTION_NODES = {"Flux2Scheduler", "CFGGuider", "RandomNoise",
                    "KSamplerSelect", "SamplerCustomAdvanced"}
FLUX2_EDIT_NODES = {"LoadImage", "ImageScaleToTotalPixels", "GetImageSize",
                    "VAEEncode", "ReferenceLatent"}
PIXEL_NODES = {"LoraLoader", "KSampler"}
H3_NODES = {
    "UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3ImageToVideo",
    "RandomNoise", "BasicScheduler", "BasicGuider", "KSamplerSelect",
    "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio", "CreateVideo",
    "SaveVideo", "LoadImage",
}

MAX_COMFY_DIMENSION = 16384


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def result_json(ok, path=None, error=None, **extra):
    result = {"ok": ok, "path": path, "error": error,
              "cost_cents": 0, "backend": "comfyui"}
    result.update(extra)
    print(json.dumps(result))
    sys.exit(0 if ok else 1)


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Tunnel:
    """SSH local-forward to the remote ComfyUI loopback port."""

    def __init__(self):
        self.local_port = _free_local_port()
        self.ctl = Path(tempfile.gettempdir()) / f"comfy-tunnel-{uuid.uuid4().hex}.sock"

    @property
    def base(self):
        return f"http://127.0.0.1:{self.local_port}"

    def __enter__(self):
        log(f"[comfyui] opening tunnel -> {SSH_HOST}:{REMOTE_API_PORT}")
        subprocess.run(
            ["ssh", "-fN", "-M", "-S", str(self.ctl),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
             "-o", "ExitOnForwardFailure=yes",
             "-L", f"{self.local_port}:127.0.0.1:{REMOTE_API_PORT}", SSH_HOST],
            check=True, timeout=20,
        )
        for _ in range(30):
            try:
                urllib.request.urlopen(self.base + "/system_stats", timeout=2)
                return self
            except Exception:
                time.sleep(0.5)
        self.__exit__(None, None, None)
        raise RuntimeError("ComfyUI tunnel did not become ready")

    def __exit__(self, *exc):
        try:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-S", str(self.ctl),
                 "-O", "exit", SSH_HOST],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            log("[comfyui] timed out while closing the SSH tunnel")


def _http_error(method, url, error):
    try:
        detail = error.read().decode(errors="replace")
    except Exception:
        detail = str(error)
    detail = detail.strip()
    if len(detail) > 2000:
        detail = detail[:2000] + "..."
    return RuntimeError(f"ComfyUI {method} {url} failed (HTTP {error.code}): {detail}")


def _get_json(base, path, timeout=30):
    try:
        return json.load(urllib.request.urlopen(base + path, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise _http_error("GET", path, e) from e


def _post_json(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise _http_error("POST", path, e) from e


def _upload_image(base, image_path):
    """POST /upload/image (multipart) and return the server-side filename."""
    image_path = Path(image_path)
    data = image_path.read_bytes()
    remote_name = f"godogen-{uuid.uuid4().hex}{image_path.suffix.lower()}"
    boundary = "----comfygen" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{remote_name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        base + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    r = json.load(urllib.request.urlopen(req, timeout=300))
    name = r["name"]
    if r.get("subfolder"):
        name = f"{r['subfolder']}/{name}"
    return name


def _submit_and_wait(base, workflow, timeout=300):
    pid = _post_json(base, "/prompt", {"prompt": workflow})["prompt_id"]
    log(f"[comfyui] queued {pid}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hist = _get_json(base, f"/history/{pid}", timeout=30)
        if pid in hist:
            for node in hist[pid]["outputs"].values():
                if "images" in node:
                    return node["images"][0]
            status = hist[pid].get("status", {})
            raise RuntimeError(f"job finished with no downloadable output: {status}")
        time.sleep(1)
    raise RuntimeError(f"job {pid} timed out after {timeout}s")


def _fetch(base, img):
    q = urllib.parse.urlencode(
        {"filename": img["filename"], "subfolder": img.get("subfolder", ""),
         "type": img["type"]})
    return urllib.request.urlopen(base + "/view?" + q, timeout=300).read()


def _basename(name):
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


class Inventory:
    """Lazy view of installed ComfyUI nodes and model-picker choices."""

    def __init__(self, base):
        self.base = base
        self._schemas = {}

    def schema(self, node):
        if node not in self._schemas:
            try:
                self._schemas[node] = _get_json(
                    self.base, "/object_info/" + urllib.parse.quote(node)).get(node)
            except RuntimeError:
                self._schemas[node] = None
        return self._schemas[node]

    def missing_nodes(self, nodes):
        return sorted(node for node in nodes if self.schema(node) is None)

    def choices(self, node, input_name):
        schema = self.schema(node)
        if not schema:
            return []
        spec = schema.get("input", {}).get("required", {}).get(input_name, [])
        if spec and isinstance(spec[0], list):
            return spec[0]
        return []

    def resolve(self, node, input_name, candidates):
        choices = self.choices(node, input_name)
        for candidate in candidates:
            for choice in choices:
                if choice == candidate or _basename(choice) == candidate:
                    return choice
        return None


def _image_status(inventory, style, mode="both"):
    profile = IMAGE_PROFILES[style]
    resolved = {"name": profile["name"], "style": style}
    nodes = set(IMAGE_COMMON_NODES)
    nodes |= PIXEL_NODES if style == "pixel" else PRODUCTION_NODES
    if mode in {"edit", "both"}:
        if not profile["supports_edit"]:
            return resolved, [f"{profile['name']} does not support reference editing"]
        nodes |= FLUX2_EDIT_NODES
    missing = inventory.missing_nodes(nodes)
    components = [
        ("unet", "UNETLoader", "unet_name"),
        ("clip", "CLIPLoader", "clip_name"),
        ("vae", "VAELoader", "vae_name"),
    ]
    if style == "pixel":
        components.append(("lora", "LoraLoader", "lora_name"))

    for component, node, input_name in components:
        value = inventory.resolve(node, input_name, profile[component])
        if value:
            resolved[component] = value
        else:
            missing.append(f"{component}: {' | '.join(profile[component])}")
    return resolved, sorted(set(missing))


def _require_image_profile(inventory, style, mode):
    resolved, missing = _image_status(inventory, style, mode)
    if not missing:
        return resolved
    raise RuntimeError(
        f"{IMAGE_PROFILES[style]['name']} is missing [{'; '.join(missing)}]")


def _h3_status(inventory):
    resolved = {"name": H3_MODEL}
    missing = inventory.missing_nodes(H3_NODES)
    components = (
        ("unet", "UNETLoader", "unet_name"),
        ("clip", "CLIPLoader", "clip_name"),
        ("video_vae", "VAELoader", "vae_name"),
        ("audio_vae", "VAELoader", "vae_name"),
    )
    for component, node, input_name in components:
        value = inventory.resolve(node, input_name, H3_PROFILE[component])
        if value:
            resolved[component] = value
        else:
            missing.append(f"{component}: {' | '.join(H3_PROFILE[component])}")
    return resolved, sorted(set(missing))


def _flux2_loaders(profile):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": profile["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": profile["clip"], "type": "flux2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": profile["vae"]}},
    }


def _flux2_sampling(workflow, positive, negative, latent, width, height,
                    seed, steps, guidance):
    workflow.update({
        "20": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "21": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": positive, "negative": negative,
            "cfg": guidance}},
        "22": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "23": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": steps, "width": width, "height": height}},
        "24": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["20", 0], "guider": ["21", 0], "sampler": ["22", 0],
            "sigmas": ["23", 0], "latent_image": latent}},
        "25": {"class_type": "VAEDecode", "inputs": {
            "samples": ["24", 0], "vae": ["3", 0]}},
        "26": {"class_type": "SaveImage", "inputs": {
            "images": ["25", 0], "filename_prefix": "godogen"}},
    })


def build_flux2_text2img(prompt, neg, width, height, seed, steps, guidance, profile):
    workflow = _flux2_loaders(profile)
    workflow.update({
        "10": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": neg}},
        "12": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
    })
    _flux2_sampling(workflow, ["10", 0], ["11", 0], ["12", 0],
                    width, height, seed, steps, guidance)
    return workflow


def build_flux2_pixel(prompt, neg, width, height, seed, steps, guidance, profile):
    workflow = _flux2_loaders(profile)
    workflow.update({
        "10": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["20", 1], "text": prompt}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["20", 1], "text": neg}},
        "12": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "20": {"class_type": "LoraLoader", "inputs": {
            "model": ["1", 0], "clip": ["2", 0],
            "lora_name": profile["lora"], "strength_model": 1.0,
            "strength_clip": 1.0}},
        "21": {"class_type": "KSampler", "inputs": {
            "model": ["20", 0], "positive": ["10", 0], "negative": ["11", 0],
            "latent_image": ["12", 0], "seed": seed, "steps": steps,
            "cfg": guidance, "sampler_name": "euler", "scheduler": "normal",
            "denoise": 1.0}},
        "22": {"class_type": "VAEDecode", "inputs": {
            "samples": ["21", 0], "vae": ["3", 0]}},
        "23": {"class_type": "SaveImage", "inputs": {
            "images": ["22", 0], "filename_prefix": "godogen_pixel"}},
    })
    return workflow


def build_flux2_edit(prompt, neg, server_image, seed, steps, guidance,
                     megapixels, profile):
    workflow = _flux2_loaders(profile)
    workflow.update({
        "4": {"class_type": "LoadImage", "inputs": {"image": server_image}},
        "5": {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["4", 0], "upscale_method": "lanczos",
            "megapixels": megapixels, "resolution_steps": 16}},
        "6": {"class_type": "GetImageSize", "inputs": {"image": ["5", 0]}},
        "10": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": neg}},
        "12": {"class_type": "VAEEncode", "inputs": {
            "pixels": ["5", 0], "vae": ["3", 0]}},
        "13": {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": ["10", 0], "latent": ["12", 0]}},
        "14": {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": ["11", 0], "latent": ["12", 0]}},
        "15": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": ["6", 0], "height": ["6", 1], "batch_size": 1}},
    })
    _flux2_sampling(workflow, ["13", 0], ["14", 0], ["15", 0],
                    ["6", 0], ["6", 1], seed, steps, guidance)
    return workflow


def _h3_frame_count(duration):
    frame_count = max(5, round(duration * 24))
    return frame_count + (5 - frame_count % 17) % 17


def _h3_canvas(source_width, source_height):
    ratio = source_width / source_height
    if ratio >= 1:
        width, height = 768 * ratio, 768
    else:
        width, height = 768, 768 / ratio
    max_pixels = 768 * 1344
    if width * height > max_pixels:
        scale = math.sqrt(max_pixels / (width * height))
        width, height = width * scale, height * scale
    return (max(32, round(width / 32) * 32),
            max(32, round(height / 32) * 32))


def _source_image_size(image_path):
    try:
        from PIL import Image
        with Image.open(image_path) as image:
            return image.size
    except ImportError as e:
        raise RuntimeError("Pillow is required to infer H3 video aspect ratio") from e


def _video_canvas(args):
    if (args.width is None) != (args.height is None):
        raise ValueError("--width and --height must be provided together")
    if args.width is not None:
        width, height = args.width, args.height
        if not (32 <= width <= MAX_COMFY_DIMENSION and
                32 <= height <= MAX_COMFY_DIMENSION):
            raise ValueError(
                f"MiniMax H3 width and height must be between 32 and {MAX_COMFY_DIMENSION}")
        if width % 32 or height % 32:
            raise ValueError("MiniMax H3 width and height must be multiples of 32")
        if width * height > 768 * 1344:
            raise ValueError("MiniMax H3 local Base canvas must not exceed 768x1344 pixels")
        return width, height
    reference = args.image or args.last_image
    if reference:
        return _h3_canvas(*_source_image_size(reference))
    return 1344, 768


def build_h3_video(prompt, width, height, frame_count, seed, steps, profile,
                   first_image=None, last_image=None):
    conditioning = {
        "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
        "width": width, "height": height, "length": frame_count,
    }
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": profile["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": profile["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": profile["video_vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": profile["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": conditioning},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "BasicScheduler", "inputs": {
            "model": ["1", 0], "scheduler": "simple", "steps": steps,
            "denoise": 1.0}},
        "8": {"class_type": "BasicGuider", "inputs": {
            "model": ["1", 0], "conditioning": ["5", 0]}},
        "9": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "res_multistep"}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["8", 0], "sampler": ["9", 0],
            "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {
            "samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {
            "images": ["11", 0], "audio": ["12", 0], "fps": 24,
            "bit_depth": 8}},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": "video/godogen_h3",
            "format": "auto", "codec": "auto"}},
    }
    if first_image:
        workflow["15"] = {"class_type": "LoadImage", "inputs": {"image": first_image}}
        conditioning["first_frame"] = ["15", 0]
    if last_image:
        workflow["16"] = {"class_type": "LoadImage", "inputs": {"image": last_image}}
        conditioning["last_frame"] = ["16", 0]
    return workflow


def _valid_image_dimensions(width, height):
    if width <= 0 or height <= 0 or width % 16 or height % 16:
        raise ValueError("image width and height must be positive multiples of 16")
    if width > MAX_COMFY_DIMENSION or height > MAX_COMFY_DIMENSION:
        raise ValueError(
            f"image width and height must not exceed {MAX_COMFY_DIMENSION}")


def cmd_image(args):
    output = Path(args.output)
    config = IMAGE_PROFILES[args.style]
    width = (config["default_size"] if args.width is None else args.width)
    height = (config["default_size"] if args.height is None else args.height)
    steps = (config["default_steps"] if args.steps is None else args.steps)
    guidance = (config["default_guidance"] if args.guidance is None
                else args.guidance)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        _valid_image_dimensions(width, height)
        if steps <= 0:
            raise ValueError("--steps must be positive")
        if guidance < 0:
            raise ValueError("--guidance must not be negative")
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if not 0.01 <= args.megapixels <= 16:
            raise ValueError("--megapixels must be between 0.01 and 16")
        if args.image and not config["supports_edit"]:
            raise ValueError("--style pixel is text-to-image only")
        with Tunnel() as tunnel:
            mode = "edit" if args.image else "text"
            profile = _require_image_profile(
                Inventory(tunnel.base), args.style, mode)
            log(f"[comfyui] image model={profile['name']} style={args.style}")
            if args.image:
                server_image = _upload_image(tunnel.base, args.image)
                workflow = build_flux2_edit(
                    args.prompt, args.negative, server_image, args.seed,
                    steps, guidance, args.megapixels, profile)
            elif args.style == "pixel":
                workflow = build_flux2_pixel(
                    args.prompt, args.negative, width, height, args.seed,
                    steps, guidance, profile)
            else:
                workflow = build_flux2_text2img(
                    args.prompt, args.negative, width, height,
                    args.seed, steps, guidance, profile)
            media = _submit_and_wait(tunnel.base, workflow, timeout=args.timeout)
            output.write_bytes(_fetch(tunnel.base, media))
    except Exception as e:
        result_json(False, error=f"{type(e).__name__}: {e}",
                    model=config["name"], style=args.style)
    result_json(True, path=str(output), model=profile["name"], style=args.style,
                width=width, height=height)


def cmd_video(args):
    if not args.accept_h3_license:
        result_json(False, error=(
            "MiniMax H3 uses a restricted community license, not an open-source license; "
            "review asset-gen/comfyui.md and pass --accept-h3-license only when eligible"),
            model=H3_MODEL)
    output = Path(args.output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not 4 <= args.duration <= 15:
            raise ValueError("MiniMax H3 duration must be between 4 and 15 seconds")
        if args.steps <= 0:
            raise ValueError("--steps must be positive")
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        width, height = _video_canvas(args)
        frame_count = _h3_frame_count(args.duration)
        with Tunnel() as tunnel:
            profile, missing = _h3_status(Inventory(tunnel.base))
            if missing:
                raise RuntimeError("MiniMax H3 stack is not ready: " + "; ".join(missing))
            first_image = _upload_image(tunnel.base, args.image) if args.image else None
            last_image = _upload_image(tunnel.base, args.last_image) if args.last_image else None
            actual_duration = frame_count / 24
            log(f"[comfyui] MiniMax H3 {width}x{height}, {frame_count} frames "
                f"({actual_duration:.3f}s), native stereo audio")
            workflow = build_h3_video(
                args.prompt, width, height, frame_count, args.seed, args.steps,
                profile, first_image, last_image)
            media = _submit_and_wait(tunnel.base, workflow, timeout=args.timeout)
            output.write_bytes(_fetch(tunnel.base, media))
    except Exception as e:
        result_json(False, error=f"{type(e).__name__}: {e}", model=H3_MODEL)
    result_json(True, path=str(output), model=H3_MODEL,
                width=width, height=height, frames=frame_count,
                duration_seconds=frame_count / 24, audio="stereo-32khz")


def cmd_doctor(args):
    try:
        with Tunnel() as tunnel:
            inventory = Inventory(tunnel.base)
            images = {}
            for style, config in IMAGE_PROFILES.items():
                _resolved, text_missing = _image_status(inventory, style, "text")
                if config["supports_edit"]:
                    _resolved, edit_missing = _image_status(inventory, style, "edit")
                    edit = {
                        "supported": True,
                        "inventory_complete": not edit_missing,
                        "missing": edit_missing,
                    }
                else:
                    edit = {"supported": False}
                images[style] = {
                    "model": config["name"],
                    "text_to_image": {
                        "inventory_complete": not text_missing,
                        "missing": text_missing,
                    },
                    "edit": edit,
                }
            _resolved, missing = _h3_status(inventory)
            h3 = {"inventory_complete": not missing, "missing": missing}
            stats = _get_json(tunnel.base, "/system_stats")
    except Exception as e:
        result_json(False, error=f"{type(e).__name__}: {e}")
    if args.capability == "backend":
        ok = True
    elif args.capability == "h3":
        ok = h3["inventory_complete"]
    else:
        image = images[args.style]
        ok = image["text_to_image"]["inventory_complete"]
        if image["edit"]["supported"]:
            ok = ok and image["edit"]["inventory_complete"]
    print(json.dumps({
        "ok": ok, "path": None, "error": None,
        "backend": "comfyui", "cost_cents": 0,
        "checked_capability": args.capability,
        "image_profiles": images, "minimax_h3": h3,
        "comfyui_version": stats.get("system", {}).get("comfyui_version"),
    }))
    sys.exit(0 if ok else 1)


def main():
    parser = argparse.ArgumentParser(
        description="Free local generation via self-hosted ComfyUI (FLUX images + MiniMax H3).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    image_parser = sub.add_parser(
        "image", help="text-to-image or reference-conditioned edit (FLUX)")
    image_parser.add_argument("--prompt", required=True)
    image_parser.add_argument("-o", "--output", required=True)
    image_parser.add_argument("--image", help="reference image for instruction editing")
    image_parser.add_argument(
        "--style", choices=tuple(IMAGE_PROFILES), default="production",
        help="production uses 4B Base; pixel uses distilled 4B + pixel LoRA")
    image_parser.add_argument("--negative", default="", help="negative prompt")
    image_parser.add_argument("--width", type=int,
                              help="default 1024 for production, 512 for pixel")
    image_parser.add_argument("--height", type=int,
                              help="default 1024 for production, 512 for pixel")
    image_parser.add_argument("--megapixels", type=float, default=1.0,
                              help="edit output area while preserving reference aspect ratio")
    image_parser.add_argument("--seed", type=int, default=42)
    image_parser.add_argument("--steps", type=int,
                              help="default 50 for production, 4 for pixel")
    image_parser.add_argument("--guidance", type=float,
                              help="default 4.0 for production, 1.0 for pixel")
    image_parser.add_argument("--timeout", type=int, default=600)
    image_parser.set_defaults(func=cmd_image)

    video_parser = sub.add_parser(
        "video", help="MiniMax H3 local video with native stereo audio")
    video_parser.add_argument("--prompt", required=True)
    video_parser.add_argument("-o", "--output", required=True)
    video_parser.add_argument("--image", help="optional first frame")
    video_parser.add_argument("--last-image", help="optional last frame")
    video_parser.add_argument("--duration", type=float, default=5.0,
                              help="requested seconds, 4-15; snapped to H3's frame grid")
    video_parser.add_argument("--width", type=int,
                              help="canvas width; set with --height, multiple of 32")
    video_parser.add_argument("--height", type=int,
                              help="canvas height; set with --width, multiple of 32")
    video_parser.add_argument("--seed", type=int, default=42)
    video_parser.add_argument("--steps", type=int, default=20)
    video_parser.add_argument("--timeout", type=int, default=3600)
    video_parser.add_argument("--accept-h3-license", action="store_true",
                              help="confirm eligibility under the MiniMax H3 community license")
    video_parser.set_defaults(func=cmd_video)

    doctor_parser = sub.add_parser(
        "doctor", help="inspect backend, installed models, and required nodes")
    doctor_parser.add_argument(
        "--capability", choices=["backend", "image", "h3"], default="backend",
        help="set the inventory check that controls the exit status")
    doctor_parser.add_argument(
        "--style", choices=tuple(IMAGE_PROFILES), default="production",
        help="image style checked by --capability image")
    doctor_parser.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
