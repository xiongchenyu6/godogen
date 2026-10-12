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
