# Latest-Only Local Game-Asset Models

The self-hosted stack carries one current model line per asset capability. Model
selection never falls back to an older checkpoint. Repository CLIs open a
short-lived SSH tunnel to loopback-only ComfyUI and return the exact model in JSON.

## GPU box

- Host `root@101.78.126.6` (`sg-office`, NixOS), 2× RTX 4090 24 GB, 128 GB RAM.
- ComfyUI is bound to `127.0.0.1:8188`.
- UI tunnel: `ssh -L 8188:127.0.0.1:8188 root@101.78.126.6`, then open
  `http://127.0.0.1:8188`.

## Model set

| Game asset | Model | Output | License | Repository integration |
|------------|-------|--------|---------|------------------------|
| General 2D, props, textures, instruction edits | [FLUX.2 Klein Base 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-base-4B) | PNG | Apache-2.0 | `comfyui_gen.py image --style production` |
| Pixel sprites | [FLUX.2 Klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) distilled + [pixel-art LoRA](https://huggingface.co/Limbicnation/pixel-art-lora) | PNG | Apache-2.0 | `comfyui_gen.py image --style pixel` |
| Video, action footage, synchronized dialogue/SFX/music | [MiniMax H3 Base FL2VA](https://huggingface.co/MiniMaxAI/MiniMax-H3) | MP4, 24 fps + 32 kHz stereo | restricted H3 community license | `comfyui_gen.py video`; explicit acceptance required |
| PBR image-to-3D | [Pixal3D](https://github.com/TencentARC/Pixal3D) | textured GLB | MIT project + DINOv3 custom runtime dependency | deployment target; third-party license review required |
| Skeleton and skin weights | [SkinTokens / TokenRig](https://github.com/VAST-AI-Research/SkinTokens) | rigged GLB | MIT project | deployment target; no animation clips |
| Game sound effects | [MOSS-SoundEffect-v2.0](https://huggingface.co/OpenMOSS-Team/MOSS-SoundEffect-v2.0) | 48 kHz WAV, up to 30 s | Apache-2.0 | deployment target |
| Dialogue and voice cloning | [MOSS-TTS Local v1.5](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5) | 48 kHz stereo WAV | Apache-2.0 | deployment target |

Gemini, Grok, and Tripo3D in `SKILL.md` remain the cloud routes while a local
capability is not live-verified.

## Inventory gate

`doctor` distinguishes backend reachability from a complete model inventory:

```bash
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py doctor
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py doctor \
  --capability image --style production
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py doctor \
  --capability image --style pixel
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py doctor --capability h3
```

An `inventory_complete` result only proves that ComfyUI exposes the required nodes
and files. A route is usable for game assets after one real output is decoded,
inspected, and loaded through the target engine's runtime asset path.

## FLUX.2 4B images

Production uses the undistilled Base checkpoint at 50 steps and guidance 4. Pixel uses
the distilled checkpoint with its matching LoRA at 4 steps and CFG 1. Both are
Apache-2.0; the pixel LoRA is not compatible with the Base checkpoint.

```text
models/diffusion_models/flux-2-klein-base-4b-fp8.safetensors
models/diffusion_models/flux-2-klein-4b-fp8.safetensors
models/text_encoders/qwen_3_4b.safetensors
models/vae/flux2-vae.safetensors
models/loras/pixel-art/pytorch_lora_weights.comfyui.safetensors
```

```bash
# general game art
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py image \
  --style production \
  --prompt "a glowing blue mana crystal game icon, centered, dark background, crisp" \
  -o ${RUNTIME_ASSET_DIR}/img/mana_icon.png

# reference-conditioned instruction edit; Base uses reference latents
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py image \
  --style production \
  --image ${RUNTIME_ASSET_DIR}/img/turret.png \
  --prompt "same turret, glowing red, battle-damaged, cracked armor" \
  -o ${RUNTIME_ASSET_DIR}/img/turret_damaged.png

# pixel sprite; defaults to 512×512
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py image \
  --style pixel \
  --prompt "pixel art sprite, armored forest guardian, front view, game asset" \
  -o ${RUNTIME_ASSET_DIR}/img/forest_guardian.png
```

Pixel mode is text-to-image only. Treat its PNG as ordinary RGB until inspection
confirms a real alpha channel; use the normal matte pipeline otherwise.

## MiniMax H3 Base video

The workflow supports text-to-video, first-frame image-to-video, and first+last-
frame video. It jointly decodes 24 fps video and 32 kHz stereo audio. The local
stack requires ComfyUI 0.30.0+ and about 42.5 GB of model files:

```text
models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors
models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
models/vae/minimax_h3_video_vae_fp16.safetensors
models/vae/minimax_h3_audio_vae_fp32.safetensors
```

Only H3 Base at its native 768-short-edge budget is local. Context-IR, the initial
sparse-attention release, and 2K regeneration are not part of this stack. Two 24 GB
cards do not automatically form one 48 GB device; offload behavior needs a real
benchmark.

H3 is **open-weight, not open source**. Its community license excludes use of the
model and outputs in named territories including the US, EU, UK, and South Korea,
and adds attribution, commercial, and hosted-use conditions. Review the
[license](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE) for the
project and distribution territory before using `--accept-h3-license`.

```bash
python3 ${ASSET_GEN_SKILL_DIR}/tools/comfyui_gen.py video \
  --image ${RUNTIME_ASSET_DIR}/img/hero_walk_pose.png \
  --prompt "the same hero walks in place; locked camera; Audio: cloth and footsteps" \
  --duration 5 --accept-h3-license -o scratch/hero_walk_h3.mp4
```

`--duration` accepts 4–15 seconds and snaps upward to H3's `17k+5` frame grid.
Explicit dimensions must be multiples of 32 and remain within the Base pixel
budget. Confirm both video and stereo audio with `ffprobe`. Official workflow:
[ComfyUI H3 guide](https://docs.comfy.org/tutorials/video/minimax/minimax-h3).

## Game-ready post-processing

For animated sprites: reference → pose → H3 → frames → loop trim → matte.
`grid_slice.py`, `find_loop_frame.py`, and `rembg_matting.py` cover slicing, cycle
selection, and transparency cleanup.

## 3D and audio deployment targets

- Pixal3D: validate the low-VRAM path, PBR export, topology, and GLB engine import;
  its TRELLIS.2 base targets at least 24 GB. Its WebP-enabled glTF export must be
  tested in all three engines, and the DINOv3 runtime dependency needs separate
  license approval.
- SkinTokens: validate texture/scale transfer and engine bone naming. Its stated
  minimum is 14 GB; use `--use_transfer` after Pixal3D to preserve texture and
  scale. The selected articulation and skin-VAE checkpoints come from
  `VAST-AI/SkinTokens`; animation clips still require a separate source.
- MOSS-SoundEffect/TTS: record peak VRAM, preserve sample-rate/channel metadata,
  and verify WAV import and looping. The separate flagship MOSS-TTS-v1.5 emits
  24 kHz audio; the selected Local-Transformer route emits 48 kHz stereo.

Pixal3D, SkinTokens, MOSS-SoundEffect, and MOSS-TTS require incompatible Python,
Torch, and CUDA stacks. Deploy them as isolated environments or services rather
than adding them to the ComfyUI environment.
