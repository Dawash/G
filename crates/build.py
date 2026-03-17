#!/usr/bin/env python3
"""Build the Rust audio-capture binary and install it to audio/bin/.

Usage::
    python crates/build.py            # release build (optimized)
    python crates/build.py --debug    # debug build (faster compile)
    python crates/build.py --check    # just verify Rust is installed
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
CRATE_DIR = os.path.join(SCRIPT_DIR, "audio-capture")
AUDIO_BIN_DIR = os.path.join(ROOT_DIR, "audio", "bin")

BINARY_NAME = "audio_capture.exe" if platform.system() == "Windows" else "audio_capture"


# ── Rust detection ─────────────────────────────────────────────────────────────

def check_rust() -> bool:
    """Return True if cargo is on PATH and functional."""
    try:
        result = subprocess.run(
            ["cargo", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            print(f"  [Rust] {result.stdout.strip()}")
            return True
        return False
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False


def get_rust_version() -> str:
    """Return the installed rustc version string, or empty string."""
    try:
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8", errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ── Build ──────────────────────────────────────────────────────────────────────

def build(debug: bool = False) -> bool:
    """Compile the crate and copy the binary to audio/bin/.

    Returns True on success, False on failure.
    """
    if not check_rust():
        print(
            "\nERROR: Rust/Cargo not found.\n"
            "Install from https://rustup.rs/\n"
            "  Windows:  winget install Rustlang.Rustup\n"
            "  Linux:    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\n"
            "  macOS:    brew install rust"
        )
        return False

    profile = "debug" if debug else "release"
    cargo_args = ["cargo", "build"] + ([] if debug else ["--release"])

    print(f"\nBuilding audio-capture ({profile})…")
    result = subprocess.run(cargo_args, cwd=CRATE_DIR)
    if result.returncode != 0:
        print("ERROR: cargo build failed — see output above")
        return False

    # Locate the compiled binary
    src = os.path.join(CRATE_DIR, "target", profile, BINARY_NAME)
    if not os.path.isfile(src):
        print(f"ERROR: Expected binary not found at: {src}")
        return False

    # Copy to audio/bin/
    os.makedirs(AUDIO_BIN_DIR, exist_ok=True)
    dst = os.path.join(AUDIO_BIN_DIR, BINARY_NAME)
    shutil.copy2(src, dst)

    # Make executable on Unix
    if platform.system() != "Windows":
        os.chmod(dst, 0o755)

    size_kb = os.path.getsize(dst) / 1024
    print(f"\n  Binary installed: {dst}  ({size_kb:.0f} KB)")
    print("  Restart the assistant to activate the Rust audio pipeline.")
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Build the G audio-capture binary")
    parser.add_argument(
        "--debug", action="store_true",
        help="Debug build (faster compile, unoptimized)"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Only check if Rust is installed (no build)"
    )
    args = parser.parse_args()

    if args.check:
        if check_rust():
            rv = get_rust_version()
            print(f"  {rv}")
            print("Rust is installed. Run without --check to build.")
            return 0
        else:
            print("Rust is NOT installed.")
            return 1

    return 0 if build(debug=args.debug) else 1


if __name__ == "__main__":
    sys.exit(main())
