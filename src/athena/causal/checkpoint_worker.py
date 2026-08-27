"""Child-process entry point for blocking checkpoint operations."""

from __future__ import annotations

import argparse
import json

from athena.causal.checkpoint import CheckpointManager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("capture", "fingerprint", "restore"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--label")
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    manager = CheckpointManager(root=args.root)
    try:
        if args.operation == "capture":
            result = manager._capture_sync(
                task_id=args.task_id or "unknown",
                workspace_root=args.workspace_root,
                label=args.label or "workspace-snapshot",
            )
        elif args.operation == "fingerprint":
            result = {
                "fingerprint": manager._fingerprint_sync(args.workspace_root)
            }
        else:
            result = manager._restore_sync(
                checkpoint_id=args.checkpoint_id or "",
                workspace_root=args.workspace_root,
                expected_fingerprint=args.expected_fingerprint,
            )
    except Exception as exc:  # noqa: BLE001 - serialize worker failures at the process boundary
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
