"""
Training-Free GRPO-style few-shot selection.

Pipeline:
1) Sample n questions, generate g trajectories per question.
2) Keep correct trajectories and run LLM-guided group computation to extract
   high-value "advantage texts" (经验).
3) Validate each advantage text on a validation split (single-text ablation).
4) Keep top-k advantage texts by validation gain.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from openai import OpenAI
from zhipuai import ZhipuAI

# Ensure project root is importable when running as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.agent_eval import (
    VectorStore,
    first_env,
    load_corpus,
    load_embeddings,
    load_env,
    run_agent,
)


def get_qid(item: Dict) -> int:
    qid = item.get("qid")
    if qid is None:
        qid = item.get("quid")
    return int(qid)


def chat_json_with_retry(
    chat_client: OpenAI,
    chat_model: str,
    messages: List[Dict],
    temperature: float = 0.0,
    retries: int = 3,
) -> str:
    err = None
    for i in range(retries):
        try:
            resp = chat_client.chat.completions.create(
                model=chat_model,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            err = exc
            if i < retries - 1:
                time.sleep(2**i)
    raise RuntimeError(f"chat request failed: {err}")


def parse_json_block(text: str) -> Dict:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}
    m2 = re.search(r"(\{.*\})", text, flags=re.S)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            return {}
    return {}


def normalize_advantage(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text


def trace_to_compact_view(trace: List[Dict], max_info_docs: int = 2) -> Dict:
    searches = []
    infos = []
    final_answer = ""
    for step in trace:
        role = step.get("role")
        if role == "search":
            q = step.get("query", "")
            if q:
                searches.append(q)
        elif role == "information":
            docs = step.get("results", [])
            mini = []
            for d in docs[:max_info_docs]:
                mini.append(d.get("text", ""))
            if mini:
                infos.append(mini)
        elif role == "answer":
            final_answer = step.get("content", "")
    return {
        "searches": searches,
        "evidence_docs_top2_per_round": infos,
        "final_answer": final_answer,
    }


def llm_group_compute(
    chat_client: OpenAI,
    chat_model: str,
    question: str,
    gold: List[str],
    correct_runs: List[Dict],
    max_advantages: int,
) -> List[Dict]:
    if not correct_runs:
        return []

    traces = []
    for i, run in enumerate(correct_runs, start=1):
        traces.append(
            {
                "run_id": i,
                "trace": trace_to_compact_view(run.get("trace", [])),
            }
        )

    system_msg = (
        "You are selecting high-value reasoning experiences for tool-using QA.\n"
        "Given multiple correct trajectories, extract only the insights that are likely to improve future accuracy.\n"
        "Do NOT output generic advice. Keep each insight concrete and reusable.\n"
        "Return JSON only: {\"advantages\":[{\"text\":\"...\",\"why\":\"...\",\"source_run\":1}]}"
    )
    user_msg = {
        "question": question,
        "gold_answers": gold,
        "correct_trajectories": traces,
        "max_advantages": max_advantages,
    }
    content = chat_json_with_retry(
        chat_client,
        chat_model,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": json.dumps(user_msg, ensure_ascii=False)},
        ],
        temperature=0.0,
    )
    obj = parse_json_block(content)
    adv = obj.get("advantages", []) if isinstance(obj, dict) else []
    out = []
    for x in adv:
        if isinstance(x, dict):
            t = normalize_advantage(str(x.get("text", "")))
            if not t:
                continue
            out.append(
                {
                    "text": t,
                    "why": str(x.get("why", "")),
                    "source_run": x.get("source_run"),
                }
            )
        elif isinstance(x, str):
            t = normalize_advantage(x)
            if t:
                out.append({"text": t, "why": "", "source_run": None})
    return out[:max_advantages]


def build_prompt(base_prompt: str, experiences: List[str]) -> str:
    if not experiences:
        return base_prompt
    lines = [base_prompt.strip(), "", "Additional Few-Shot Experiences:"]
    for i, exp in enumerate(experiences, start=1):
        lines.append(f"{i}. {exp}")
    return "\n".join(lines).strip()


def eval_accuracy(
    questions: List[Dict],
    chat_client: OpenAI,
    embed_client: ZhipuAI,
    vector_store: VectorStore,
    corpus: List[str],
    dates,
    chat_model: str,
    system_prompt: str,
    top_k: int,
    max_steps: int,
    concurrency: int,
) -> Tuple[float, List[Dict]]:
    results = [None] * len(questions)
    correct = 0

    def worker(iq):
        i, q = iq
        r = run_agent(
            chat_client=chat_client,
            embed_client=embed_client,
            vector_store=vector_store,
            corpus=corpus,
            dates_ord=dates,
            question=q["question"],
            gold=q["answers"],
            embed_model="embedding-3",
            chat_model=chat_model,
            top_k=top_k,
            max_steps=max_steps,
            system_prompt=system_prompt,
        )
        return i, q, r

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(worker, (i, q)) for i, q in enumerate(questions)]
        for fut in as_completed(futures):
            i, q, r = fut.result()
            results[i] = {
                "qid": get_qid(q),
                "question": q["question"],
                "gold": q["answers"],
                **r,
            }
            correct += int(r["correct"])

    acc = correct / len(questions) if questions else 0.0
    return acc, results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    ap.add_argument("--chat-model", default="deepseek-chat")
    ap.add_argument("--train-file", type=Path, default=Path("MultiTQ/questions/train.json"))
    ap.add_argument("--val-file", type=Path, default=Path("MultiTQ/questions/dev.json"))
    ap.add_argument("--base-prompt", type=Path, default=Path("eval/prompts/system.md"))
    ap.add_argument("--experience-bank", type=Path, default=Path("training-free-grpo/experience_bank.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("training-free-grpo/runs"))
    ap.add_argument("--n", type=int, default=32, help="batch size (number of questions)")
    ap.add_argument("--g", type=int, default=4, help="group size (responses per question)")
    ap.add_argument("--k", type=int, default=8, help="max size of experience bank")
    ap.add_argument("--max-advantages-per-group", type=int, default=2)
    ap.add_argument("--val-sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--max-steps", type=int, default=20)
    args = ap.parse_args()

    load_env(Path(".env"))
    api_key = first_env(["DEEPSEEK_API_KEY", "API_KEY", "api_key", "OPENAI_API_KEY"])
    if not api_key:
        raise SystemExit("Missing chat API key in .env")
    zhipu_key = first_env(["ZHIPU_API_KEY"])
    if not zhipu_key:
        raise SystemExit("Missing ZHIPU_API_KEY in .env")

    chat_client = OpenAI(api_key=api_key, base_url=args.base_url.rstrip("/") + "/v1")
    embed_client = ZhipuAI(api_key=zhipu_key)

    base_prompt = args.base_prompt.read_text(encoding="utf-8")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    train = json.loads(args.train_file.read_text(encoding="utf-8"))
    dev = json.loads(args.val_file.read_text(encoding="utf-8"))
    random.seed(args.seed)
    train_batch = random.sample(train, k=min(args.n, len(train)))
    val_batch = random.sample(dev, k=min(args.val_sample, len(dev)))

    corpus, dates, heads, tails, rels = load_corpus(Path("MultiTQ/kg/full.txt"))
    emb = load_embeddings(Path("MultiTQ/embeddings/full_norm256_conc"))
    store = VectorStore(emb, dates_ordinal=dates, heads_norm=heads, tails_norm=tails, rels_norm=rels)

    # Load existing bank (if any)
    if args.experience_bank.exists():
        obj = json.loads(args.experience_bank.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            existing_bank = [normalize_advantage(x) for x in obj.get("experiences", [])]
        else:
            existing_bank = [normalize_advantage(x) for x in obj]
    else:
        existing_bank = []

    # Stage 1: n*g trajectories
    stage1 = []
    print(f"[stage1] generating trajectories: n={len(train_batch)}, g={args.g}")
    for q in train_batch:
        q_runs = []
        for rid in range(args.g):
            r = run_agent(
                chat_client=chat_client,
                embed_client=embed_client,
                vector_store=store,
                corpus=corpus,
                dates_ord=dates,
                question=q["question"],
                gold=q["answers"],
                embed_model="embedding-3",
                chat_model=args.chat_model,
                top_k=args.top_k,
                max_steps=args.max_steps,
                system_prompt=build_prompt(base_prompt, existing_bank),
            )
            q_runs.append(r)
        stage1.append(
            {
                "qid": get_qid(q),
                "question": q["question"],
                "gold": q["answers"],
                "runs": q_runs,
                "correct_count": sum(int(x["correct"]) for x in q_runs),
            }
        )
    (args.out_dir / "stage1_candidate_runs.json").write_text(
        json.dumps(stage1, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stage 2: LLM-guided group computation on correct runs
    print("[stage2] group computation for advantage texts")
    candidate_advantages = []
    for item in stage1:
        correct_runs = [x for x in item["runs"] if x.get("correct")]
        adv = llm_group_compute(
            chat_client=chat_client,
            chat_model=args.chat_model,
            question=item["question"],
            gold=item["gold"],
            correct_runs=correct_runs,
            max_advantages=args.max_advantages_per_group,
        )
        candidate_advantages.append(
            {
                "qid": item["qid"],
                "question": item["question"],
                "correct_count": item["correct_count"],
                "advantages": adv,
            }
        )
    (args.out_dir / "stage2_group_advantages.json").write_text(
        json.dumps(candidate_advantages, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Flatten + deduplicate
    pool_new = []
    seen = set()
    for group in candidate_advantages:
        for a in group["advantages"]:
            t = normalize_advantage(a["text"])
            if t and t not in seen:
                seen.add(t)
                pool_new.append(t)

    pool_all = []
    for t in existing_bank + pool_new:
        nt = normalize_advantage(t)
        if nt and nt not in pool_all:
            pool_all.append(nt)

    # Stage 3: validation scoring
    print("[stage3] validation scoring")
    baseline_prompt = build_prompt(base_prompt, [])
    baseline_acc, _ = eval_accuracy(
        val_batch,
        chat_client=chat_client,
        embed_client=embed_client,
        vector_store=store,
        corpus=corpus,
        dates=dates,
        chat_model=args.chat_model,
        system_prompt=baseline_prompt,
        top_k=args.top_k,
        max_steps=args.max_steps,
        concurrency=args.concurrency,
    )

    scored = []
    for idx, adv in enumerate(pool_all, start=1):
        p = build_prompt(base_prompt, [adv])
        acc, _ = eval_accuracy(
            val_batch,
            chat_client=chat_client,
            embed_client=embed_client,
            vector_store=store,
            corpus=corpus,
            dates=dates,
            chat_model=args.chat_model,
            system_prompt=p,
            top_k=args.top_k,
            max_steps=args.max_steps,
            concurrency=args.concurrency,
        )
        scored.append(
            {
                "rank_eval_index": idx,
                "text": adv,
                "acc": acc,
                "delta_vs_baseline": acc - baseline_acc,
            }
        )
        print(f"  [{idx}/{len(pool_all)}] delta={acc - baseline_acc:+.4f}")

    scored.sort(key=lambda x: x["delta_vs_baseline"], reverse=True)
    selected = scored[: args.k]
    selected_texts = [x["text"] for x in selected]

    # Final prompt test (selected library together)
    final_prompt = build_prompt(base_prompt, selected_texts)
    final_acc, _ = eval_accuracy(
        val_batch,
        chat_client=chat_client,
        embed_client=embed_client,
        vector_store=store,
        corpus=corpus,
        dates=dates,
        chat_model=args.chat_model,
        system_prompt=final_prompt,
        top_k=args.top_k,
        max_steps=args.max_steps,
        concurrency=args.concurrency,
    )

    report = {
        "config": vars(args),
        "train_batch_size": len(train_batch),
        "val_batch_size": len(val_batch),
        "existing_bank_size": len(existing_bank),
        "new_candidate_count": len(pool_new),
        "candidate_pool_total": len(pool_all),
        "baseline_acc": baseline_acc,
        "selected_topk": selected,
        "selected_library_size": len(selected_texts),
        "final_selected_library_acc": final_acc,
    }

    (args.out_dir / "stage3_validation_scores.json").write_text(
        json.dumps(scored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    args.experience_bank.parent.mkdir(parents=True, exist_ok=True)
    args.experience_bank.write_text(
        json.dumps({"experiences": selected_texts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("DONE")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
