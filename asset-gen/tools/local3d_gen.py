#!/usr/bin/env python3
"""Free local image-to-3D via the self-hosted TRELLIS.2 service.

Talks to the TRELLIS.2 HTTP API on the GPU box, which is bound to the remote
host's loopback behind Nginx, by opening a short-lived SSH local-forward. No API
keys, no per-generation charge. Nginx balances across one worker per RTX 4090,
so two assets generate concurrently; a single asset is not twice as fast.

Subcommands:
  glb     image-to-3D, returns a textured PBR GLB
  doctor  report whether the service answers and which GPU each worker holds

Output: JSON to stdout {"ok": true, "path": "...", "cost_cents": 0, "backend": "trellis2"}.
Progress goes to stderr — redirect it and read only on failure.

This route produces geometry and PBR textures, never a skeleton or animation
clips. Rigged and retargeted characters stay on Tripo3D in asset_gen.py. See
local3d.md for the service contract, the gate, and post-processing.
"""
import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Self-hosted GPU box (sg-office, 2x RTX 4090). The TRELLIS.2 API is
# loopback-only on the remote, so we reach it through an SSH tunnel.
SSH_HOST = "root@101.78.126.6"
# Nginx balances the workers; each worker also answers directly, which is the
# only way to prove that they hold different GPUs.
REMOTE_API_PORT = 8080
REMOTE_WORKER_PORT_BASE = 8000

MODEL = "trellis2-4b"
MAX_IMAGE_BYTES = 30 * 1024 * 1024
TEXTURE_SIZES = (1024, 2048, 4096)
FACE_LIMIT_RANGE = (5_000, 1_000_000)

# Face and texture budgets per asset role. The service accepts anything in
# FACE_LIMIT_RANGE; these are the values that survive engine import without a
# retopology pass.
PRESETS = {
    "distant": {"face_limit": 12_000, "texture_size": 1024},
    "prop": {"face_limit": 20_000, "texture_size": 1024},
    "weapon": {"face_limit": 50_000, "texture_size": 2048},
    "hero": {"face_limit": 100_000, "texture_size": 2048},
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def result_json(ok, path=None, error=None, **extra):
    result = {"ok": ok, "path": path, "error": error,
              "cost_cents": 0, "backend": "trellis2"}
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
    """SSH local-forwards to the remote TRELLIS.2 loopback ports.

    `remote_ports[0]` is the entry point exposed as `base`; the rest are
    addressed by `url_for`.
    """

    def __init__(self, remote_ports=(REMOTE_API_PORT,)):
        self.remote_ports = tuple(remote_ports)
        self.ports = {remote: _free_local_port() for remote in self.remote_ports}
        self.ctl = Path(tempfile.gettempdir()) / f"trellis-tunnel-{uuid.uuid4().hex}.sock"

    def url_for(self, remote_port):
        return f"http://127.0.0.1:{self.ports[remote_port]}"

    @property
    def base(self):
        return self.url_for(self.remote_ports[0])

    def __enter__(self):
        log(f"[trellis2] opening tunnel -> {SSH_HOST}:{list(self.remote_ports)}")
        forwards = []
        for remote, local in self.ports.items():
            forwards += ["-L", f"{local}:127.0.0.1:{remote}"]
        subprocess.run(
            ["ssh", "-fN", "-M", "-S", str(self.ctl),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
             "-o", "ExitOnForwardFailure=yes", *forwards, SSH_HOST],
            check=True, timeout=20,
        )
        for _ in range(30):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=2)
                return self
            except urllib.error.HTTPError:
                return self
            except Exception:
                time.sleep(0.5)
        self.__exit__(None, None, None)
        raise RuntimeError(
            "TRELLIS.2 did not answer /health through the tunnel; the service is "
            "not deployed or not running on the GPU box")

    def __exit__(self, *exc):
        try:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-S", str(self.ctl),
                 "-O", "exit", SSH_HOST],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            log("[trellis2] timed out while closing the SSH tunnel")


def _http_error(method, url, error):
    try:
        detail = error.read().decode(errors="replace")
    except Exception:
        detail = str(error)
    detail = detail.strip()
    if len(detail) > 2000:
        detail = detail[:2000] + "..."
    return RuntimeError(f"TRELLIS.2 {method} {url} failed (HTTP {error.code}): {detail}")


def _get_json(base, path, timeout=30):
    try:
        return json.load(urllib.request.urlopen(base + path, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise _http_error("GET", path, e) from e


def _health(base):
    """/health from whichever worker Nginx picked for this request."""
    health = _get_json(base, "/health", timeout=15)
    if health.get("status") != "ok":
        raise RuntimeError(f"unexpected /health payload: {json.dumps(health)[:500]}")
    if "gpu" not in health:
        raise RuntimeError(
            "port %d answers /health but is not the TRELLIS.2 API "
            "(no gpu field): %s" % (REMOTE_API_PORT, json.dumps(health)[:500]))
    return health


def _submit(base, image_path, face_limit, texture_size, timeout):
    """POST /v1/image-to-3d (multipart). The API is synchronous — one long request."""
    image_path = Path(image_path)
    data = image_path.read_bytes()
    if not data:
        raise ValueError(f"{image_path} is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{image_path} is larger than the service's 30 MiB limit")

    boundary = "----local3d" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{image_path.name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data, b"\r\n",
    ]
    for field, value in (("face_limit", face_limit), ("texture_size", texture_size)):
        parts += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode(),
        ]
    parts.append(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        base + "/v1/image-to-3d", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        if e.code == 507:
            raise RuntimeError(
                "worker ran out of VRAM; retry with --preset prop or a smaller "
                "--texture-size, and check that nothing else holds that GPU") from e
        raise _http_error("POST", "/v1/image-to-3d", e) from e


def cmd_glb(args):
    output = Path(args.output)
    preset = PRESETS[args.preset]
    face_limit = args.face_limit or preset["face_limit"]
    texture_size = args.texture_size or preset["texture_size"]
    try:
        if not Path(args.image).exists():
            raise FileNotFoundError(args.image)
        if not FACE_LIMIT_RANGE[0] <= face_limit <= FACE_LIMIT_RANGE[1]:
            raise ValueError("--face-limit must be between %d and %d" % FACE_LIMIT_RANGE)
        if texture_size not in TEXTURE_SIZES:
            raise ValueError(f"--texture-size must be one of {TEXTURE_SIZES}")
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        output.parent.mkdir(parents=True, exist_ok=True)
        with Tunnel() as tunnel:
            health = _health(tunnel.base)
            log(f"[trellis2] {health['gpu']} (device {health.get('visible_devices')}), "
                f"{face_limit} faces, {texture_size}px textures")
            job = _submit(tunnel.base, args.image, face_limit, texture_size, args.timeout)
            if job.get("status") != "succeeded" or not job.get("url"):
                raise RuntimeError(f"job did not succeed: {json.dumps(job)[:500]}")
            log(f"[trellis2] job {job['id']} done, downloading GLB")
            glb = urllib.request.urlopen(tunnel.base + job["url"], timeout=300).read()
            output.write_bytes(glb)
    except Exception as e:
        result_json(False, error=f"{type(e).__name__}: {e}", model=MODEL)
    result_json(True, path=str(output), model=MODEL, preset=args.preset,
                face_limit=face_limit, texture_size=texture_size,
                bytes=output.stat().st_size)


def cmd_doctor(args):
    """Check the balancer, then each worker port directly.

    Two workers pinned to different GPUs is the whole point of the deployment;
    a balancer that answers proves nothing about which cards are live.
    """
    worker_ports = [REMOTE_WORKER_PORT_BASE + i for i in range(args.workers)]
    workers = {}
    try:
        with Tunnel([REMOTE_API_PORT, *worker_ports]) as tunnel:
            balancer = _health(tunnel.base)
            for port in worker_ports:
                try:
                    health = _health(tunnel.url_for(port))
                    workers[port] = {
                        "gpu": health["gpu"],
                        "visible_devices": health.get("visible_devices"),
                        "allocated_mb": health.get("allocated_mb"),
                    }
                except Exception as e:
                    workers[port] = {"error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        result_json(False, error=f"{type(e).__name__}: {e}", model=MODEL)
    live = [p for p, w in workers.items() if "error" not in w]
    devices = sorted({str(workers[p]["visible_devices"]) for p in live})
    ok = len(live) == args.workers and len(devices) == args.workers
    print(json.dumps({
        "ok": ok, "path": None, "cost_cents": 0, "backend": "trellis2",
        "model": MODEL,
        "error": None if ok else (
            f"{len(live)}/{args.workers} workers answered, on device(s) {devices}; "
            "each worker needs its own CUDA_VISIBLE_DEVICES"),
        "balancer": balancer,
        "expected_workers": args.workers,
        "workers": {str(p): w for p, w in workers.items()},
    }))
    sys.exit(0 if ok else 1)


def main():
    parser = argparse.ArgumentParser(
        description="Free local image-to-3D via the self-hosted TRELLIS.2 service.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    glb_parser = sub.add_parser("glb", help="image-to-3D, textured PBR GLB")
    glb_parser.add_argument("--image", required=True,
                            help="reference PNG; do not matte it, keep the solid background")
    glb_parser.add_argument("-o", "--output", required=True)
    glb_parser.add_argument("--preset", choices=tuple(PRESETS), default="prop",
                            help="face and texture budget by asset role (default prop)")
    glb_parser.add_argument("--face-limit", type=int,
                            help="override the preset's decimation target")
    glb_parser.add_argument("--texture-size", type=int, choices=TEXTURE_SIZES,
                            help="override the preset's texture resolution")
    glb_parser.add_argument("--timeout", type=int, default=1800,
                            help="the API is synchronous; this covers the whole generation")
    glb_parser.set_defaults(func=cmd_glb)

    doctor_parser = sub.add_parser(
        "doctor", help="check the service and that each worker holds its own GPU")
    doctor_parser.add_argument("--workers", type=int, default=2,
                               help="expected worker count, one per GPU")
    doctor_parser.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
