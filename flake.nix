# SPDX-FileCopyrightText: 2021 Serokell <https://serokell.io/>
#
# SPDX-License-Identifier: CC0-1.0
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs =
    { nixpkgs, flake-parts, ... }@inputs:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      perSystem =
        {
          config,
          self',
          inputs',
          pkgs,
          system,
          lib,
          ...
        }:
        {
          devShells.default =
            with pkgs;
            let
              # asset-gen's local routes (comfyui_gen.py, local3d_gen.py) reach the
              # GPU box over SSH with the standard library alone; Pillow is what
              # they need on top of it, and numpy backs the frame tools. The paid
              # cloud SDKs and rembg stay on pip — they are not in nixpkgs.
              assetGenPython = python3.withPackages (ps: [
                ps.pillow
                ps.numpy
                ps.requests
              ]);
            in
            mkShell.override { stdenv = pkgs.clangStdenv; } {
              RUST_SRC_PATH = "${pkgs.rust.packages.stable.rustPlatform.rustLibSrc}";
              RUST_BACKTRACE = 1;

              # Native system libraries Bevy links against (needed to build
              # rustdoc via ./setup_bevy_docs.sh: wayland-sys, x11, xkbcommon,
              # alsa-sys, libudev-sys, vulkan all run pkg-config at build time).
              buildInputs = [
                wayland
                libxkbcommon
                alsa-lib
                udev
                vulkan-loader
                xorg.libX11
                xorg.libXcursor
                xorg.libXrandr
                xorg.libXi
              ];
              nativeBuildInputs = [
                pkg-config
                nixfmt
                nixd
                rustc
                cargo
                rust-analyzer
                clippy
                openssl
                rustfmt
                assetGenPython
                ffmpeg
                imagemagick
              ];

              LD_LIBRARY_PATH = lib.makeLibraryPath [
                wayland
                libxkbcommon
                vulkan-loader
              ];
            };
        };
    };
}
