"""
Run zero-shot pass@k on MultiTQ test set with chunked execution.

Definition used here:
- Sample N questions once (fixed seed).
- Round 1 evaluates all sampled questions.
- Round i (i>1) only re-evaluates questions not solved in previous rounds.
- pass@k is cumulative solved_ratio after each round.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


def get_qid(item: Dict) -> int:
    qid = item.get("qid")
    if qid is None:
        qid = item.get("quid")
    if qid is None:
        raise ValueError(f"Missing qid/quid in item: {item}")
    return int(qid)


def load_or_sample_questions(
    questions_file: Path,
    sample_path: Path,
    sample_n: int,
    seed: int,
) -> List[Dict]:
    if sample_path.exists():
        return json.loads(sample_path.read_text(encoding="utf-8"))

    data = json.loads(questions_file.read_text(encoding="utf-8"))
    random.seed(seed)
    sample = random.sample(data, k=min(sample_n, len(data)))
    sample_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    return sample


def run() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    ap.add_argument("--chat-model", default="deepseek-chat")
    ap.add_argument("--questions-file", type=Path, default=Path("MultiTQ/questions/test.json"))
    # pass@k is explicitly zero-shot by default
    ap.add_argument("--prompt-path", type=Path, default=Path("eval/prompts/system.md"))
    ap.add_argument("--agent-script", type=Path, default=Path("eval/agent_eval.py"))
    ap.add_argument("--out-dir", type=Path, default=Path("MultiTQ/eval_runs"))
    ap.add_argument("--prefix", default="passk_zero_shot_clean")
    ap.add_argument("--sample-n", type=int, default=3000)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--chunk-size", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--python-exec", default=sys.executable)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    sample_path = out_dir / f"{args.prefix}_sample{args.sample_n}.json"
    summary_path = out_dir / f"{args.prefix}_summary.json"
    sample = load_or_sample_questions(args.questions_file, sample_path, args.sample_n, args.seed)

    sample_qids = [get_qid(x) for x in sample]
    qid_to_q = {get_qid(x): x for x in sample}
    total = len(sample_qids)

    solved_total = set()
    pass_at_k = []
    round_stats = []

    for rnd in range(1, args.k + 1):
        solved_before = set(solved_total)
        round_targets = [qid_to_q[qid] for qid in sample_qids if qid not in solved_before]

        part_outputs = []
        for i in range(0, len(round_targets), args.chunk_size):
            part_idx = i // args.chunk_size + 1
            out_name = f"{args.prefix}_round{rnd:02d}_part{part_idx:03d}"
            out_json = out_dir / f"{out_name}.json"
            q_json = out_dir / f"{out_name}_questions.json"
            part_outputs.append(out_json)

            if args.resume and out_json.exists():
                continue

            chunk = round_targets[i : i + args.chunk_size]
            q_json.write_text(json.dumps(chunk, ensure_ascii=False, indent=2), encoding="utf-8")

            cmd = [
                args.python_exec,
                str(args.agent_script),
                "--base-url",
                args.base_url,
                "--chat-model",
                args.chat_model,
                "--questions-file",
                str(q_json),
                "--num",
                str(len(chunk)),
                "--system-prompt",
                str(args.prompt_path),
                "--concurrency",
                str(args.concurrency),
                "--out-name",
                out_name,
            ]
            print(f"[round {rnd}] run {out_name}, size={len(chunk)}")
            subprocess.run(cmd, check=True)

        correct_in_round = set()
        attempted = 0
        for out_json in part_outputs:
            if not out_json.exists():
                raise RuntimeError(f"Missing output file: {out_json}")
            obj = json.loads(out_json.read_text(encoding="utf-8"))
            results = obj.get("results", [])
            attempted += len(results)
            for r in results:
                if r.get("correct"):
                    correct_in_round.add(int(r.get("qid")))

        solved_total.update(correct_in_round)
        new_correct = len(solved_total) - len(solved_before)
        p = len(solved_total) / total if total > 0 else 0.0
        pass_at_k.append(p)
        info = {
            "round": rnd,
            "attempted": attempted,
            "new_correct": new_correct,
            "solved_total": len(solved_total),
            "remaining_after_round": total - len(solved_total),
            "pass_at_round": p,
        }
        round_stats.append(info)
        print(f"[round {rnd}] pass@{rnd}={p:.6f}, new_correct={new_correct}, remaining={info['remaining_after_round']}")

        summary = {
            "sample_n": total,
            "seed": args.seed,
            "k": args.k,
            "chunk_size": args.chunk_size,
            "concurrency": args.concurrency,
            "base_url": args.base_url,
            "chat_model": args.chat_model,
            "prompt_path": str(args.prompt_path),
            "prefix": args.prefix,
            "pass_at_k": pass_at_k,
            "final_solved": len(solved_total),
            "round_stats": round_stats,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("DONE")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
