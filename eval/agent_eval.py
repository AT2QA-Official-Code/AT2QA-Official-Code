"""
End-to-end agent evaluation on MultiTQ using upgraded search filters.

Behavior:
- DeepSeek-V3.2 can call `vector_search` with optional date window, sort mode,
  entity (head/tail) filter, and relation filter.
- Retrieval uses GLM-Embedding-3 (256 dim) over full.txt embeddings.
- Saves raw and pretty traces with search parameters.

Run (from repo root):
  conda run -n Multi-RAG --no-capture-output python eval/agent_eval.py \
    --base-url https://llmapi.paratera.com \
    --num 200 --questions-file MultiTQ/eval_runs/new_200.json

Env fallbacks:
  Chat API key: DEEPSEEK_API_KEY / API_KEY / api_key / OPENAI_API_KEY
  Embedding key: ZHIPU_API_KEY
  Base URL: BASE_URL / GLM_BASE_URL
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
from openai import OpenAI, APIConnectionError, APITimeoutError
from zhipuai import ZhipuAI


# ------------ utilities ------------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def first_env(keys: List[str]) -> str | None:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return None


# ------------ data loading ------------
def load_questions(num: int, path: Path, random_sample: bool = False) -> List[Dict]:
    import random

    data = json.loads(path.read_text(encoding="utf-8"))
    if random_sample:
        return random.sample(data, k=min(num, len(data)))
    return data[:num]


def _normalize_entity(text: str) -> str:
    return " ".join(text.replace("_", " ").lower().split())


def load_corpus(path: Path) -> Tuple[List[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lines = path.read_text(encoding="utf-8").splitlines()
    dates = []
    heads = []
    tails = []
    rels = []
    from datetime import date
    for line in lines:
        parts = line.split("\t")
        head = parts[0] if len(parts) > 0 else ""
        rel = parts[1] if len(parts) > 1 else ""
        tail = parts[2] if len(parts) > 2 else ""
        heads.append(_normalize_entity(head))
        rels.append(_normalize_entity(rel))
        tails.append(_normalize_entity(tail))
        try:
            d = parts[3] if len(parts) > 3 else line.rsplit("\t", 1)[-1]
            dates.append(date.fromisoformat(d).toordinal())
        except Exception:
            dates.append(0)
    return (
        lines,
        np.asarray(dates, dtype=np.int64),
        np.asarray(heads),
        np.asarray(tails),
        np.asarray(rels),
    )


def load_embeddings(emb_dir: Path = Path("MultiTQ/embeddings/full_norm256_conc")) -> np.memmap:
    stats = json.loads((emb_dir / "stats.json").read_text(encoding="utf-8"))
    rows = stats["total_lines"]
    dim = stats["dim"]
    emb_path = emb_dir / stats.get("embeddings_path", "embedding-3.f32.npy")
    return np.memmap(emb_path, dtype="float32", mode="r", shape=(rows, dim))


# ------------ retrieval ------------
class VectorStore:
    def __init__(
        self,
        embeddings: np.ndarray,
        dates_ordinal: Optional[np.ndarray] = None,
        heads_norm: Optional[np.ndarray] = None,
        tails_norm: Optional[np.ndarray] = None,
        rels_norm: Optional[np.ndarray] = None,
    ):
        self.emb = embeddings
        self.emb_norm = np.linalg.norm(self.emb, axis=1)
        self.dim = embeddings.shape[1]
        self.dates = dates_ordinal
        self.heads = heads_norm
        self.tails = tails_norm
        self.rels = rels_norm

    def search(
        self,
        query_vec: np.ndarray,
        top_k: int = 5,
        start_ord: Optional[int] = None,
        end_ord: Optional[int] = None,
        sort_mode: str = "relevance",
        entity_filter_name: Optional[str] = None,
        entity_filter_pos: Optional[str] = None,
        relation_filter: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        qn = np.linalg.norm(query_vec)
        sims = (self.emb @ query_vec) / (self.emb_norm * qn + 1e-8)
        # date filter
        mask = np.ones_like(sims, dtype=bool)
        if self.dates is not None and (start_ord or end_ord):
            if start_ord:
                mask &= self.dates >= start_ord
            if end_ord:
                mask &= self.dates <= end_ord
        # relation filter
        if relation_filter and self.rels is not None:
            rel_norm = _normalize_entity(relation_filter)
            mask &= self.rels == rel_norm
        # entity filter
        if entity_filter_name and self.heads is not None and self.tails is not None:
            ent_norm = _normalize_entity(entity_filter_name)
            if entity_filter_pos == "front":
                mask &= self.heads == ent_norm
            elif entity_filter_pos == "back":
                mask &= self.tails == ent_norm
        if not mask.any():
            return []
        sims_filtered = np.where(mask, sims, -np.inf)

        # relevance ranking (keep up to 50 before secondary time ordering)
        keep = min(50, np.count_nonzero(mask))
        if keep == 0:
            return []
        idx_rel = np.argpartition(-sims_filtered, range(keep))[:keep]
        idx_rel = idx_rel[np.argsort(-sims_filtered[idx_rel])]

        # optional time sort on top-keep
        if sort_mode == "time_asc":
            idx_sorted = sorted(
                idx_rel,
                key=lambda i: (self.dates[i] if self.dates is not None else 0, -sims_filtered[i]),
            )
        elif sort_mode == "time_desc":
            idx_sorted = sorted(
                idx_rel,
                key=lambda i: (-(self.dates[i] if self.dates is not None else 0), -sims_filtered[i]),
            )
        else:
            idx_sorted = idx_rel

        idx_final = idx_sorted[:top_k]
        return [(int(i), float(sims[i])) for i in idx_final if sims_filtered[i] != -np.inf]


def _extract_embedding(resp) -> List[float]:
    data = None
    if hasattr(resp, "data"):
        data = resp.data
    elif isinstance(resp, dict):
        data = resp.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if isinstance(item, dict):
            return item.get("embedding", [])
        if hasattr(item, "embedding"):
            return item.embedding
    if isinstance(resp, dict) and "embedding" in resp:
        return resp["embedding"]
    raise ValueError("embedding not found in response")


def embed_query(client: ZhipuAI, model: str, text: str, dimensions: int) -> np.ndarray:
    for attempt in range(3):
        try:
            resp = client.embeddings.create(model=model, input=[text], dimensions=dimensions)
            emb = _extract_embedding(resp)
            return np.asarray(emb, dtype="float32")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


# ------------ evaluation ------------
def is_correct(pred: str, gold: List[str]) -> bool:
    import re

    answers = re.findall(r"<answer>(.*?)</answer>", pred, flags=re.IGNORECASE | re.DOTALL)
    if answers:
        text = " ".join(a.strip() for a in answers)
    else:
        text = pred
    text = text.lower()

    # Original relaxed rule: any gold contained is enough.
    for g in gold:
        g_l = g.lower()
        if g_l in text or text in g_l:
            return True
    return False


def run_agent(
    chat_client: OpenAI,
    embed_client: ZhipuAI,
    vector_store: VectorStore,
    corpus: List[str],
    dates_ord: np.ndarray,
    question: str,
    gold: List[str],
    embed_model: str,
    chat_model: str,
    top_k: int = 10,
    max_steps: int = 20,
    system_prompt: str = "",
) -> Dict:
    def chat_with_retry(messages):
        for attempt in range(3):
            try:
                return chat_client.chat.completions.create(
                    model=chat_model,
                    messages=messages,
                    tools=tool_spec,
                    tool_choice="auto",
                )
            except (APIConnectionError, APITimeoutError) as exc:
                sleep = 2 ** attempt
                if attempt == 2:
                    raise
                time.sleep(sleep)
    tool_spec = [
        {
            "type": "function",
            "function": {
                "name": "vector_search",
                "description": "Search the knowledge base.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "start_date": {"type": "string", "description": "optional ISO date YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "optional ISO date YYYY-MM-DD"},
                        "sort": {"type": "string", "description": "relevance (default), time_asc, time_desc"},
                        "entity_filter_name": {"type": "string", "description": "Entity string to filter on head/tail"},
                        "entity_filter_position": {
                            "type": "string",
                            "enum": ["front", "back"],
                            "description": "front=head entity, back=tail entity",
                        },
                        "relation_filter": {"type": "string", "description": "Relation name filter"},
                    },
                    "required": ["query"],
                },
            },
        }
    ]

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {"role": "user", "content": question},
    ]

    trace = [{"role": "user", "content": question}]
    used_tool = False
    answer = ""
    # entity legality checking disabled

    for step_i in range(max_steps):
        resp = chat_with_retry(messages)
        assistant_msg = resp.choices[0].message
        tool_calls = assistant_msg.tool_calls
        last_info_results = None

        if tool_calls:
            used_tool = True
            trace.append(
                {
                    "role": "assistant_think",
                    "content": assistant_msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in tool_calls
                    ],
                }
            )
            messages.append(assistant_msg)

            tool_msgs = []
            for tc in tool_calls:
                try:
                    args_dict = json.loads(tc.function.arguments)
                except Exception:
                    trace.append({"role": "error", "message": f"bad_tool_args: {tc.function.arguments}"})
                    args_dict = {"query": question}
                qtext = args_dict.get("query", question)
                start_date = args_dict.get("start_date")
                end_date = args_dict.get("end_date")
                sort_mode = args_dict.get("sort", "relevance")
                ent_name = args_dict.get("entity_filter_name")
                ent_pos = args_dict.get("entity_filter_position")
                rel_filter = args_dict.get("relation_filter")
                trace.append(
                    {
                        "role": "search",
                        "query": qtext,
                        "sort": sort_mode,
                        "start": start_date,
                        "end": end_date,
                        "entity_filter_name": ent_name,
                        "entity_filter_position": ent_pos,
                        "relation_filter": rel_filter,
                    }
                )
                try:
                    qvec = embed_query(embed_client, embed_model, qtext, vector_store.dim)
                except Exception as exc:
                    trace.append({"role": "error", "message": f"embed_failed: {exc}"})
                    results = []
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(results, ensure_ascii=False),
                        "tool_name": "vector_search",
                        "tool_args": {
                            "query": qtext,
                            "start_date": start_date,
                            "end_date": end_date,
                            "sort": sort_mode,
                            "entity_filter_name": ent_name,
                            "entity_filter_position": ent_pos,
                            "relation_filter": rel_filter,
                        },
                        "results": results,
                        "start_date": start_date,
                        "end_date": end_date,
                    }
                    tool_msgs.append(tool_msg)
                    trace.append({"role": "information", "results": results})
                    last_info_results = results
                    continue
                s_ord = e_ord = None
                from datetime import date
                try:
                    if start_date:
                        s_ord = date.fromisoformat(start_date).toordinal()
                    if end_date:
                        e_ord = date.fromisoformat(end_date).toordinal()
                except Exception:
                    s_ord = e_ord = None
                hits = vector_store.search(
                    qvec,
                    top_k=top_k,
                    start_ord=s_ord,
                    end_ord=e_ord,
                    sort_mode=sort_mode,
                    entity_filter_name=ent_name,
                    entity_filter_pos=ent_pos,
                    relation_filter=rel_filter,
                )
                results = []
                for idx, score in hits:
                    line_no = idx + 1
                    results.append(
                        {
                            "line_no": line_no,
                            "score": score,
                            "text": corpus[idx],
                        }
                    )
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(results, ensure_ascii=False),
                    "tool_name": "vector_search",
                    "tool_args": {
                        "query": qtext,
                        "start_date": start_date,
                        "end_date": end_date,
                        "sort": sort_mode,
                        "entity_filter_name": ent_name,
                        "entity_filter_position": ent_pos,
                        "relation_filter": rel_filter,
                    },
                    "results": results,
                    "start_date": start_date,
                    "end_date": end_date,
                }
                tool_msgs.append(tool_msg)
                trace.append({"role": "information", "results": results})
                last_info_results = results

            messages.extend(tool_msgs)
            continue
        else:
            answer = assistant_msg.content or ""
            import re
            answers = re.findall(r"<answer>(.*?)</answer>", answer, flags=re.I | re.S)
            answers = answers or [answer]
            # if empty answer, force retry
            if all(a.strip() == "" for a in answers):
                messages.append(assistant_msg)
                messages.append(
                    {
                        "role": "system",
                        "content": "Your reply was empty. Provide the final answer inside <answer>...</answer> only.",
                    }
                )
                trace.append({"role": "answer_empty"})
                continue

            trace.append({"role": "answer", "content": answer})
            break

    if not answer:
        answer = "(no final answer)"
    return {
        "answer": answer,
        "correct": is_correct(answer, gold),
        "used_tool": used_tool,
        "trace": trace,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=100, help="number of questions to evaluate")
    parser.add_argument("--base-url", dest="base_url", required=False)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=20, help="max dialogue turns (assistant messages)")
    parser.add_argument("--concurrency", type=int, default=12, help="number of parallel questions to run")
    parser.add_argument("--emb-dir", type=Path, default=Path("MultiTQ/embeddings/full_norm256_conc"), help="embedding directory containing stats.json")
    parser.add_argument("--chat-model", type=str, default="deepseek-chat", help="chat model name")
    parser.add_argument("--corpus-file", type=Path, default=Path("MultiTQ/kg/full.txt"), help="corpus text file matching embeddings")
    parser.add_argument("--system-prompt", type=Path, default=Path("eval/prompts/system_with_fewshot.md"), help="system prompt file")
    parser.add_argument("--questions-file", type=Path, default=Path("MultiTQ/questions/test.json"), help="questions json file")
    parser.add_argument("--random-sample", action="store_true", help="sample questions randomly instead of first N")
    parser.add_argument("--out-name", type=str, default="run_newtool", help="output name prefix")
    args = parser.parse_args()

    load_env(Path(".env"))
    api_key = first_env(["DEEPSEEK_API_KEY", "API_KEY", "api_key", "OPENAI_API_KEY"])
    base_url = args.base_url or first_env(["BASE_URL", "GLM_BASE_URL"])
    if not api_key or not base_url:
        raise SystemExit("Missing API key or base URL")

    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = base_url + "/v1"
    chat_client = OpenAI(api_key=api_key, base_url=base_url)
    zhipu_key = first_env(["ZHIPU_API_KEY"])
    if not zhipu_key:
        raise SystemExit("Missing ZHIPU_API_KEY for embeddings")
    embed_client = ZhipuAI(api_key=zhipu_key)

    system_prompt = args.system_prompt.read_text(encoding="utf-8") if args.system_prompt.exists() else ""
    entity_vocab_norm = None

    questions = load_questions(args.num, args.questions_file, random_sample=args.random_sample)
    corpus, dates_ord, heads_norm, tails_norm, rels_norm = load_corpus(args.corpus_file)
    embeddings = load_embeddings(args.emb_dir)
    store = VectorStore(embeddings, dates_ord, heads_norm=heads_norm, tails_norm=tails_norm, rels_norm=rels_norm)

    # Parallel execution
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(questions)
    correct = 0
    start = time.time()

    def worker(idx_item):
        idx, item = idx_item
        try:
            r = run_agent(
                chat_client=chat_client,
                embed_client=embed_client,
                vector_store=store,
                corpus=corpus,
                dates_ord=dates_ord,
                question=item["question"],
                gold=item["answers"],
                embed_model="embedding-3",
                chat_model=args.chat_model,
                top_k=args.top_k,
                max_steps=args.max_steps,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            r = {
                "answer": "(error)",
                "correct": False,
                "used_tool": False,
                "trace": [{"role": "error", "message": str(exc)}],
            }
        return idx, item, r

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futures = [ex.submit(worker, (i, item)) for i, item in enumerate(questions)]
        done = 0
        for fut in as_completed(futures):
            idx, item, r = fut.result()
            results[idx] = {
                "qid": item["quid"],
                "question": item["question"],
                "gold": item["answers"],
                **r,
            }
            correct += int(r["correct"])
            done += 1
            print(f"[{done}/{args.num}] correct={correct}/{done} ({correct/done:.3f}) | used_tool={r['used_tool']}")

    acc = correct / args.num
    duration = time.time() - start
    out_dir = Path("MultiTQ/eval_runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.out_name}.json"
    out_path.write_text(json.dumps({"acc": acc, "duration_sec": duration, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")

    # Build pretty trace with tags similar to the requested format.
    def to_tagged_steps(res: Dict) -> Dict:
        steps = []
        render_lines = []
        for step in res["trace"]:
            role = step.get("role")
            if role == "assistant_think":
                text = step.get("content", "")
                steps.append({"tag": "think", "text": text})
                render_lines.append(f"<think> {text} </think>")
                for tc in step.get("tool_calls", []):
                    try:
                        tc_args = json.loads(tc["arguments"])
                    except Exception:
                        tc_args = {}
                    q = tc_args.get("query", "")
                    s = tc_args.get("start_date")
                    e = tc_args.get("end_date")
                    sort_mode = tc_args.get("sort")
                    ent_name = tc_args.get("entity_filter_name")
                    ent_pos = tc_args.get("entity_filter_position")
                    rel_filter = tc_args.get("relation_filter")
                    rng = []
                    if sort_mode:
                        rng.append(f"sort={sort_mode}")
                    if s:
                        rng.append(f"start={s}")
                    if e:
                        rng.append(f"end={e}")
                    if ent_name:
                        rng.append(f"entity={ent_name}")
                    if ent_pos:
                        rng.append(f"pos={ent_pos}")
                    if rel_filter:
                        rng.append(f"rel={rel_filter}")
                    suffix = " " + " ".join(rng) if rng else ""
                    steps.append(
                        {
                            "tag": "search",
                            "text": q,
                            "start_date": s,
                            "end_date": e,
                            "sort": sort_mode,
                            "entity_filter_name": ent_name,
                            "entity_filter_position": ent_pos,
                            "relation_filter": rel_filter,
                        }
                    )
                    render_lines.append(f"<search> {q}{suffix} </search>")
            elif role == "search":
                q = step.get("query", "")
                s = step.get("start")
                e = step.get("end")
                sort_mode = step.get("sort")
                ent_name = step.get("entity_filter_name")
                ent_pos = step.get("entity_filter_position")
                rel_filter = step.get("relation_filter")
                steps.append(
                    {
                        "tag": "search",
                        "text": q,
                        "start": s,
                        "end": e,
                        "sort": sort_mode,
                        "entity_filter_name": ent_name,
                        "entity_filter_position": ent_pos,
                        "relation_filter": rel_filter,
                    }
                )
                extra = []
                if sort_mode:
                    extra.append(f"sort={sort_mode}")
                if s:
                    extra.append(f"start={s}")
                if e:
                    extra.append(f"end={e}")
                if ent_name:
                    extra.append(f"entity={ent_name}")
                if ent_pos:
                    extra.append(f"pos={ent_pos}")
                if rel_filter:
                    extra.append(f"rel={rel_filter}")
                suffix = " " + " ".join(extra) if extra else ""
                render_lines.append(f"<search> {q}{suffix} </search>")
            elif role == "information":
                results_txt = []
                for j, doc in enumerate(step.get("results", []), start=1):
                    results_txt.append(f"Doc {j}: (line {doc['line_no']}, score {doc['score']:.3f}) {doc['text']}")
                info = "\n".join(results_txt)
                steps.append({"tag": "information", "text": info})
                render_lines.append(f"<information> {info} </information>")
            elif role == "answer":
                text = step.get("content", "")
                steps.append({"tag": "answer", "text": text})
                render_lines.append(f"<answer> {text} </answer>")
        return {
            "qid": res["qid"],
            "question": res["question"],
            "gold": res["gold"],
            "correct": res["correct"],
            "steps": steps,
            "render": "\n".join(render_lines),
        }

    pretty = [to_tagged_steps(r) for r in results]
    pretty_path = out_dir / f"{args.out_name}_pretty.json"
    pretty_path.write_text(json.dumps({"acc": acc, "duration_sec": duration, "dialogues": pretty}, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Done. Accuracy={acc:.3f}, duration={duration/60:.1f} min, saved to {out_path} and {pretty_path}")


if __name__ == "__main__":
    main()
