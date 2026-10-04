#!/usr/bin/env python3
"""Install a built wheel side-by-side and execute both packaged entrypoints."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ai_agent_runtime.deployment.install import install_runtime_release
from ai_agent_runtime.process_safety import file_sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="ai-agent-runtime-wheel-smoke-") as raw_root:
        root = Path(raw_root)
        report = install_runtime_release(
            wheel=wheel,
            wheel_sha256=file_sha256(wheel),
            releases_root=root / "releases",
            release_id="wheel-smoke",
            python=Path(sys.executable),
        )
        runtime = Path(str(report["runtime_executable"]))
        wrapper = Path(str(report["wrapper_executable"]))
        subprocess.run(
            (str(runtime), "--help"),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=30,
        )
        version = subprocess.run(
            (str(wrapper), "--version"),
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not version.startswith("codex-wrapper "):
            raise RuntimeError("installed wrapper returned an unexpected version response")
        if (runtime.parents[1] / ".installing").exists():
            raise RuntimeError("installed release retained its incomplete marker")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "wheel_sha256": report["wheel_sha256"],
                    "runtime_entrypoint": True,
                    "wrapper_version": version,
                    "activation_performed": report["activation_performed"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
