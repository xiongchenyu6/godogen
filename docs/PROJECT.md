# Godogen — From Prompt to Playable Game

Godogen turns a natural-language game brief into a playable Godot, Bevy, or Babylon.js project. The agent builds the game, generates assets, runs the engine, and proves the result from the running game.

It is not a game engine, a code generator, or an asset marketplace. It is a source repo that publishes a thin runtime — a manifest, an engine guide, and an asset skill — into a fresh game repo that Claude Code or Codex then builds in.

## Source Model

The repo is organized by engine, with the cross-engine pieces shared:

- `prompts/runtime.md` — the runtime manifest
- `asset-gen/` — the `asset-gen` skill
- `engines/babylon.md`, `engines/godot.md`, `engines/bevy.md` — per-engine guides

Engine and host agent are selected at render time:

```bash
./publish.sh --engine godot   --agent claude --out ~/game
./publish.sh --engine babylon --agent codex  --out ~/game
```

Publishing writes `CLAUDE.md` + `.claude/skills/` for Claude Code, or `AGENTS.md` + `.agents/skills/` for Codex, plus the `<engine>.md` guide. Codex `agents/openai.yaml` is generated from the `asset-gen` `SKILL.md` frontmatter.

## How a run works

The runtime manifest is short: read the brief, build the game, keep durable status in `README.md`, generate assets with `asset-gen`, and follow the engine guide for stack, project sketch, and capture. There is no fixed multi-stage pipeline and no prescribed document protocol — a capable model plans and decomposes the work itself. The two things the manifest fixes are *where durable state lives* (`README.md`, so a run survives compaction) and *that the result is proven from the running game*.

The engine guide carries only what the model can't infer or discover quickly: the project sketch (what stack and layout to stand up), the capture recipe (how to render the running game headlessly), and the handful of silent-failure traps that pass a compile but break at runtime.

## Delivery

The agent decides in-run how to involve the user, reading it from how the task is framed. A task phrased as an open-ended direction gets the live game early — a Babylon.js URL, or a Godot/Bevy project they run — with the user steering at decisions of taste, scope, or cost. A task handed over as a finished brief doesn't block on anyone: the agent makes reasonable calls, finishes, and closes with a 15–20s recording of the running game, which it watches back before calling the work done.

The manifest states only this intent; everything about *how to show and capture* the game lives in the engine guide, so both paths come free on any engine.

## Engine Support

- **Godot** — Godot 4 C#/.NET. Scenes are generated at build time by headless `SceneTree` scripts; the guide carries the serialization rules (owner chain, GLB-recursion trap, post-pack validation) and the `--write-movie` + ffmpeg capture recipe.
- **Bevy** — Rust, current stable Bevy resolved and pinned at build time, ECS scenes spawned `OnEnter`. The guide points the agent at the installed source for current APIs and gives the offscreen `RenderTarget::Image` capture recipe.
- **Babylon.js** — TypeScript/Vite, served at a live URL. The guide covers the side-effect-import trap, Havok physics, and headless Chrome capture.

## Asset Generation

`asset-gen` selects the narrowest verified route for each asset instead of treating one backend as universal:

- **Local 2D** — FLUX.2 Klein 4B Base handles production images and edits; the distilled 4B checkpoint plus a pixel-art LoRA handles sprites.
- **Local video** — MiniMax H3 is available only when its license gate and live output check pass.
- **Local image-to-3D** — TRELLIS.2 produces textured PBR GLBs through a separate NixOS service. Its weights stay in host RAM and move onto a free RTX 4090 only for generation, leaving both cards available to ComfyUI while idle.
- **Cloud fallback** — Gemini and Grok cover image precision, iteration, and video when a local gate is closed. Tripo3D covers static GLBs and remains the only working route for rigged characters and retargeted animation clips.

A reachable endpoint is not considered usable on its own. Each local route has a doctor check for service and model inventory, followed by a real output smoke test before the runtime may select it. Model licenses are part of the gate.

## What Makes This Different

**Proof over claims.** A run is judged on the running game — a recorded clip or a live URL — not on code that compiles.

**Trust the model.** The runtime ships no scaffold and no planner. The model recreates boilerplate from a short sketch and decomposes the work itself; the guides spend their words only on what it genuinely can't know.

**Cost- and license-aware asset generation.** Live-verified local routes are preferred, restricted weights stay gated, and cloud providers cover capabilities the local stack has not proven. The asset manifest in `README.md` tracks paths, in-game sizes, and paid costs so implementation doesn't lose them.

**One source, many targets.** Engine and host agent are render-time choices over one source tree.

## Runtime Limitations

The runtime does not yet ship a working local rigging, sound-effect, or text-to-speech command, nor mobile/native packaging. SkinTokens and MOSS are deployment targets rather than selectable runtime routes. Video backends may emit synchronized audio inside their clips.
