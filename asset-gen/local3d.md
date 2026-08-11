# Local Image-to-3D (TRELLIS.2)

[TRELLIS.2](https://github.com/microsoft/TRELLIS.2) (MIT, `microsoft/TRELLIS.2-4B`)
is the local PBR image-to-3D line. It is a **deployment target** — the service is
declared but not switched on. Until `doctor` passes, 3D goes to Tripo3D in
`SKILL.md`.

It supersedes Pixal3D as the local 3D route: Pixal3D wraps this same TRELLIS.2
base and pulls in a DINOv3 runtime dependency with its own license terms, so the
upstream model is the shorter path to a textured GLB.

## Service contract

One worker per RTX 4090, each pinned to its card by GPU **UUID** (indices drift
when the driver re-enumerates), behind an Nginx `least_conn` balancer on the
remote loopback:

```text
tools/local3d_gen.py ──SSH tunnel──> nginx :8090 ─┬─ worker :8091 ── GPU 0
                                                  └─ worker :8092 ── GPU 1
```

The box is NixOS, so this is a `services.trellis2` module rather than a conda
prefix: the model, the five CUDA extensions it imports (`o_voxel`, `cumesh`,
`flex_gemm`, `nvdiffrast`, `nvdiffrec` renderutils), and the HTTP server are one
pinned Python environment. Port 8090 rather than the upstream 8080 — that one
belongs to another service on this host.

Two 24 GB cards are not one 48 GB card. TRELLIS.2 needs 24 GB, so a single card
holds exactly one job — the second GPU doubles throughput, not per-asset speed.
Each worker must run one Uvicorn process with no `--workers` flag, and must hold
a generation lock, or two concurrent requests on one card OOM.

The API is synchronous: `POST /v1/image-to-3d` (multipart `file`, `face_limit`,
`texture_size`) blocks until the GLB exists, then returns
`{id, status, url}`; `GET /v1/files/<id>.glb` downloads it. Both workers write
to a shared output directory, so any worker can serve any completed file.

## Gate

```bash
python3 ${ASSET_GEN_SKILL_DIR}/tools/local3d_gen.py doctor
```

`doctor` checks the balancer and then each worker port directly, because a
balancer that answers proves nothing about which cards are live. It fails unless
every worker reports its own `visible_devices`. Passing `doctor` only means the
service runs — the route is usable for game assets after one GLB is opened in
Blender and imported through the target engine's runtime asset path.

## Generation

```bash
python3 ${ASSET_GEN_SKILL_DIR}/tools/local3d_gen.py glb \
  --image ${RUNTIME_ASSET_DIR}/img/crate.png \
  --preset prop \
  -o ${RUNTIME_ASSET_DIR}/glb/crate.glb
```

Source image rules match Tripo3D: 3/4 elevated angle, solid white/gray
background, matte finish, opaque glass, single centered subject, and **do not**
matte it — the background is what separates the subject.

| `--preset` | Faces | Texture | Use |
|-----------|------:|--------:|-----|
| `distant` | 12,000 | 1024 | Scenery and background dressing |
| `prop` (default) | 20,000 | 1024 | Small scene props |
| `weapon` | 50,000 | 2048 | Hero props and weapons |
| `hero` | 100,000 | 2048 | Main-character prototypes |

`--face-limit` (5,000–1,000,000) and `--texture-size` (1024/2048/4096) override a
preset. A 507 response means that worker ran out of VRAM mid-job — drop the
preset or texture size rather than retrying identically, and check that nothing
else took the card.

## What this route does not produce

No skeleton, no skin weights, no animation clips. Rigged and retargeted
characters stay on Tripo3D (`asset_gen.py rig` / `retarget`); SkinTokens remains
a rigging deployment target in `comfyui.md`.

Decimated AI topology is not animation topology. Before binding a character,
check the edge flow around joints — a clean auto-decimated mesh still deforms
badly. Mobile and WebGPU targets need explicit LODs and a separate collision
mesh; the GLB is a starting asset, not a shipping one.

## Deployment notes

The service shares the box with ComfyUI, which claims both cards and needs all
24 GB for MiniMax H3. A TRELLIS.2 worker resident-loads its model and never
gives the memory back, so running both at once is an OOM rather than a slowdown.
That is why the service ships disabled: enabling it means either scheduling the
two against each other or giving each one card.

The API carries no authentication. Keep it on the loopback behind the SSH tunnel;
if it is ever bound to a routable address, it needs TLS and at least a bearer
token in front of it. Outputs are pruned on a daily timer.

TRELLIS.2 and the TripoSG/TripoSR fallbacks are MIT-licensed, which does not
clear the input images, so generated characters and branded shapes still need
their own review.
