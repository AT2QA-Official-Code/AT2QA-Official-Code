from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, required=True, help="summary json from run_passk_zero_shot.py")
    ap.add_argument("--out-png", type=Path, required=True)
    args = ap.parse_args()

    obj = json.loads(args.summary.read_text(encoding="utf-8"))
    y = obj.get("pass_at_k", [])
    x = list(range(1, len(y) + 1))

    plt.figure(figsize=(6.4, 4.2))
    plt.plot(x, y, marker="o")
    plt.xticks(x)
    plt.ylim(0, 1)
    plt.xlabel("k")
    plt.ylabel("pass@k")
    plt.title("Zero-shot pass@k")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

    args.out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(args.out_png, dpi=200)
    print(f"saved: {args.out_png}")


if __name__ == "__main__":
    main()

