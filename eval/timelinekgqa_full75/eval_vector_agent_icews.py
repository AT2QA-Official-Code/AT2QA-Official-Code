from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Tuple

import numpy as np
from openai import OpenAI

import eval_grep_agent as base

MIN_ORD = date(1, 1, 1).toordinal()
MAX_ORD = date(9999, 12, 31).toordinal()


def normalize_embed_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return "https://open.bigmodel.cn/api/paas/v4/"
    if u.endswith("/v1"):
        return u + "/"
    return u + "/"


def _extract_embedding(resp: Any) -> List[float]:
    data = None
    if hasattr(resp, "data"):
        data = resp.data
    elif isinstance(resp, dict):
        data = resp.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if isinstance(item, dict):
            emb = item.get("embedding")
            if isinstance(emb, list):
                return emb
        if hasattr(item, "embedding"):
            emb = item.embedding
            if isinstance(emb, list):
                return emb
    raise ValueError("embedding not found in response")


def _norm_entity(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def _norm_predicate(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def _time_to_ord(value: str) -> int | None:
    s = (value or "").strip().strip("'\"")
    if not s:
        return None
    s = s.strip("()[]{}")
    low = s.casefold()
    if low == "beginning of time":
        return MIN_ORD
    if low == "end of time":
        return MAX_ORD
    if "T" in s:
        s = s.split("T", 1)[0]
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        s = s[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().toordinal()
    except Exception:
        return None


class VectorTool:
    def __init__(
        self,
        *,
        emb_dir: Path,
        embed_api_key: str,
        embed_base_url: str,
        embed_model: str,
        dim_override: int | None = None,
    ) -> None:
        stats = json.loads((emb_dir / "stats.json").read_text(encoding="utf-8"))
        rows = int(stats["total_rows"])
        dim = int(dim_override or stats["dim"])
        emb_path = emb_dir / stats.get("embeddings_path", "embedding-3.f32.npy")
        meta_path = emb_dir / stats.get("metadata_path", "embedding-3.meta.jsonl")

        self.emb = np.memmap(emb_path, dtype="float32", mode="r", shape=(rows, dim))
        self.emb_norm = np.linalg.norm(self.emb, axis=1) + 1e-8
        self.rows = rows
        self.dim = dim
        self.embed_api_key = embed_api_key
        self.embed_base_url = normalize_embed_base_url(embed_base_url)
        self.embed_model = embed_model
        self._local = threading.local()

        self.records: List[Dict[str, Any]] = []
        self.subject_to_indices: Dict[str, List[int]] = defaultdict(list)
        self.object_to_indices: Dict[str, List[int]] = defaultdict(list)
        self.predicate_to_indices: Dict[str, List[int]] = defaultdict(list)
        start_ord_values: List[int] = []
        end_ord_values: List[int] = []
        with meta_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                text = str(raw.get("text", ""))
                parsed = base.parse_kg_csv_line(text)
                row_idx = len(self.records)
                subject_norm = _norm_entity(str(parsed.get("subject", "")))
                object_norm = _norm_entity(str(parsed.get("object", "")))
                predicate_norm = _norm_predicate(str(parsed.get("predicate", "")))
                start_ord = _time_to_ord(str(parsed.get("start_time", "")))
                end_ord = _time_to_ord(str(parsed.get("end_time", "")))
                if start_ord is None:
                    start_ord = MIN_ORD
                if end_ord is None:
                    end_ord = MAX_ORD
                if start_ord > end_ord:
                    start_ord, end_ord = end_ord, start_ord
                if subject_norm:
                    self.subject_to_indices[subject_norm].append(row_idx)
                if object_norm:
                    self.object_to_indices[object_norm].append(row_idx)
                if predicate_norm:
                    self.predicate_to_indices[predicate_norm].append(row_idx)
                self.records.append(
                    {
                        "line_no": int(raw.get("csv_line_no", -1)),
                        "parsed": parsed,
                    }
                )
                start_ord_values.append(start_ord)
                end_ord_values.append(end_ord)
        if len(self.records) != self.rows:
            raise SystemExit(
                f"Metadata rows mismatch: meta={len(self.records)} vs embeddings={self.rows}"
            )
        # Freeze postings for faster numeric filtering.
        self.subject_to_indices = {
            k: np.asarray(v, dtype=np.int64) for k, v in self.subject_to_indices.items()
        }
        self.object_to_indices = {
            k: np.asarray(v, dtype=np.int64) for k, v in self.object_to_indices.items()
        }
        self.predicate_to_indices = {
            k: np.asarray(v, dtype=np.int64)
            for k, v in self.predicate_to_indices.items()
        }
        self.start_ord = np.asarray(start_ord_values, dtype=np.int32)
        self.end_ord = np.asarray(end_ord_values, dtype=np.int32)

    def _get_client(self) -> OpenAI:
        client = getattr(self._local, "embed_client", None)
        if client is None:
            client = OpenAI(
                api_key=self.embed_api_key,
                base_url=self.embed_base_url,
                timeout=60,
            )
            self._local.embed_client = client
        return client

    def embed_query(self, text: str) -> np.ndarray:
        text = (text or "").strip()
        if not text:
            return np.zeros((self.dim,), dtype=np.float32)
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                resp = self._get_client().embeddings.create(
                    model=self.embed_model,
                    input=[text],
                    dimensions=self.dim,
                )
                emb = _extract_embedding(resp)
                return np.asarray(emb, dtype=np.float32)
            except Exception as err:
                last_err = err
                if attempt == 4:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError(f"embed failed: {last_err}")

    def _candidate_indices(
        self,
        query_subject: str = "",
        query_object: str = "",
        query_predicate: str = "",
        query_start_ord: int | None = None,
        query_end_ord: int | None = None,
        time_filter_mode: str = "overlap",
    ) -> np.ndarray:
        subject_norm = _norm_entity(query_subject)
        object_norm = _norm_entity(query_object)
        predicate_norm = _norm_predicate(query_predicate)

        idx_subject = None
        idx_object = None
        idx_predicate = None
        if subject_norm:
            idx_subject = self.subject_to_indices.get(subject_norm)
            if idx_subject is None:
                return np.asarray([], dtype=np.int64)
        if object_norm:
            idx_object = self.object_to_indices.get(object_norm)
            if idx_object is None:
                return np.asarray([], dtype=np.int64)
        if predicate_norm:
            idx_predicate = self.predicate_to_indices.get(predicate_norm)
            if idx_predicate is None:
                return np.asarray([], dtype=np.int64)

        index_parts: List[np.ndarray] = []
        if idx_subject is not None:
            index_parts.append(idx_subject)
        if idx_object is not None:
            index_parts.append(idx_object)
        if idx_predicate is not None:
            index_parts.append(idx_predicate)
        if not index_parts:
            base_idx = np.arange(self.rows, dtype=np.int64)
        else:
            base_idx = index_parts[0]
            for part in index_parts[1:]:
                base_idx = np.intersect1d(base_idx, part, assume_unique=False)
                if base_idx.size == 0:
                    return base_idx

        if base_idx.size == 0:
            return base_idx

        q_start = MIN_ORD if query_start_ord is None else int(query_start_ord)
        q_end = MAX_ORD if query_end_ord is None else int(query_end_ord)
        if q_start == MIN_ORD and q_end == MAX_ORD:
            return base_idx

        mode = (time_filter_mode or "overlap").strip().lower()
        cand_start = self.start_ord[base_idx]
        cand_end = self.end_ord[base_idx]
        if mode == "within":
            mask = (cand_start >= q_start) & (cand_end <= q_end)
        else:
            mask = (cand_end >= q_start) & (cand_start <= q_end)
        return base_idx[mask]

    def search(
        self,
        pattern: str,
        max_results: int = 30,
        query_subject: str = "",
        query_object: str = "",
        query_predicate: str = "",
        query_start: str = "",
        query_end: str = "",
        time_filter_mode: str = "overlap",
    ) -> Dict[str, Any]:
        pattern = (pattern or "").strip()
        if not pattern:
            return {"error": "empty pattern", "matches": []}

        query_start = (query_start or "").strip()
        query_end = (query_end or "").strip()
        query_start_ord = None
        query_end_ord = None
        if query_start:
            query_start_ord = _time_to_ord(query_start)
            if query_start_ord is None:
                return {"error": f"invalid query_start: {query_start}", "matches": []}
        if query_end:
            query_end_ord = _time_to_ord(query_end)
            if query_end_ord is None:
                return {"error": f"invalid query_end: {query_end}", "matches": []}
        if (
            query_start_ord is not None
            and query_end_ord is not None
            and query_start_ord > query_end_ord
        ):
            return {"error": "query_start is after query_end", "matches": []}

        mode = (time_filter_mode or "overlap").strip().lower()
        if mode not in {"overlap", "within"}:
            mode = "overlap"

        cap = min(max(1, int(max_results)), 80)
        candidates = self._candidate_indices(
            query_subject=query_subject,
            query_object=query_object,
            query_predicate=query_predicate,
            query_start_ord=query_start_ord,
            query_end_ord=query_end_ord,
            time_filter_mode=mode,
        )
        if candidates.size == 0:
            return {
                "pattern": pattern,
                "filters": {
                    "query_subject": query_subject,
                    "query_object": query_object,
                    "query_predicate": query_predicate,
                    "query_start": query_start,
                    "query_end": query_end,
                    "time_filter_mode": mode,
                    "candidate_pool": 0,
                },
                "matches": [],
            }

        q = self.embed_query(pattern)
        qn = float(np.linalg.norm(q) + 1e-8)
        emb_cand = self.emb[candidates]
        norm_cand = self.emb_norm[candidates]
        sims = (emb_cand @ q) / (norm_cand * qn)

        if cap >= len(sims):
            loc_idx = np.argsort(-sims)
        else:
            top_loc = np.argpartition(-sims, cap - 1)[:cap]
            loc_idx = top_loc[np.argsort(-sims[top_loc])]
        idx = candidates[loc_idx]

        matches: List[Dict[str, Any]] = []
        for rank, i in enumerate(idx[:cap], 1):
            rec = self.records[int(i)]
            parsed = rec["parsed"]
            matches.append(
                {
                    "line_no": rec["line_no"],
                    **parsed,
                    "score": float(
                        sims[loc_idx[rank - 1]]
                        if rank - 1 < len(loc_idx)
                        else 0.0
                    ),
                    "rank": rank,
                }
            )
        return {
            "pattern": pattern,
            "filters": {
                "query_subject": query_subject,
                "query_object": query_object,
                "query_predicate": query_predicate,
                "query_start": query_start,
                "query_end": query_end,
                "time_filter_mode": mode,
                "candidate_pool": int(candidates.size),
            },
            "matches": matches,
        }


def run_one_question(
    client: OpenAI,
    vector_tool: VectorTool,
    *,
    model: str,
    kg_file: Path,
    question: str,
    question_type: str,
    answer_type: str,
    prompt_config: Dict[str, str],
    max_steps: int,
    tool_max_results: int,
    format_max_retries: int,
    disable_entity_filter: bool,
    disable_relation_filter: bool,
) -> Dict[str, Any]:
    tool_name = "vector_search_kg"
    system_prompt = prompt_config["system_prompt"].replace("grep_kg", tool_name)
    system_prompt += (
        "\nTime filter tip: if a time window is known, pass query_start/query_end "
        "(YYYY-MM-DD, ISO timestamp, or beginning/end of time)."
    )
    if not disable_relation_filter:
        system_prompt += (
            "\nRelation filter tip: if the relation is explicit (e.g., Affiliation To), "
            "pass query_predicate for exact filtering."
        )
    if not disable_entity_filter:
        system_prompt += (
            "\nTool usage tip: when a concrete subject or object is explicitly mentioned in the question, "
            "use query_subject/query_object for exact filtering to reduce distractors."
        )
    expected = base.infer_expected_format(question)
    user_content = base.render_prompt_template(
        prompt_config["user_prompt_template"], question, answer_type
    )
    user_content = (
        f"{user_content}\n\n"
        f"Answer-format requirement (inferred from wording): {expected['instruction']}"
    )
    continue_hint = base.render_prompt_template(
        prompt_config["continue_prompt"], question, answer_type
    ).replace("grep_kg", tool_name)

    properties: Dict[str, Any] = {
        "pattern": {
            "type": "string",
            "description": "Natural-language query for vector retrieval.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum rows to return (1-80).",
            "default": 30,
        },
        "query_start": {
            "type": "string",
            "description": "Optional lower time bound for filtering (inclusive).",
        },
        "query_end": {
            "type": "string",
            "description": "Optional upper time bound for filtering (inclusive).",
        },
        "time_filter_mode": {
            "type": "string",
            "description": "Time filter mode: overlap (default) or within.",
            "default": "overlap",
        },
    }
    if not disable_relation_filter:
        properties["query_predicate"] = {
            "type": "string",
            "description": "Optional exact predicate filter (case-insensitive).",
        }
    if not disable_entity_filter:
        properties["query_subject"] = {
            "type": "string",
            "description": "Optional exact subject filter (case-insensitive).",
        }
        properties["query_object"] = {
            "type": "string",
            "description": "Optional exact object filter (case-insensitive).",
        }

    tool_spec: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": (
                    "Semantic vector search on unified_kg_icews_actor.csv rows using "
                    "GLM embedding vectors."
                ),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["pattern"],
                },
            },
        }
    ]

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    trace: List[Dict[str, Any]] = [{"role": "user", "content": question}]
    tool_calls_count = 0
    last_response = ""
    termination_reason = "max_steps"
    format_retry_count = 0

    for _ in range(max_steps):
        resp = base.chat_with_retry(client, model=model, messages=messages, tools=tool_spec)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        reasoning_content = getattr(msg, "reasoning_content", None)

        if tool_calls:
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [],
            }
            if reasoning_content is not None:
                assistant_msg["reasoning_content"] = reasoning_content
            for tc in tool_calls:
                assistant_msg["tool_calls"].append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
            messages.append(assistant_msg)

            for tc in tool_calls:
                tool_calls_count += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {"pattern": str(args)}
                except Exception:
                    args = {"pattern": str(tc.function.arguments or "")}

                pattern = str(args.get("pattern") or args.get("query") or "").strip()
                max_results = int(args.get("max_results", tool_max_results))
                if "max_results" not in args:
                    max_results = tool_max_results
                if disable_entity_filter:
                    query_subject = ""
                    query_object = ""
                else:
                    query_subject = str(
                        args.get("query_subject")
                        or args.get("subject_filter")
                        or args.get("subject")
                        or ""
                    ).strip()
                    query_object = str(
                        args.get("query_object")
                        or args.get("object_filter")
                        or args.get("object")
                        or ""
                    ).strip()
                if disable_relation_filter:
                    query_predicate = ""
                else:
                    query_predicate = str(
                        args.get("query_predicate")
                        or args.get("predicate_filter")
                        or args.get("predicate")
                        or args.get("relation")
                        or ""
                    ).strip()
                query_start = str(
                    args.get("query_start")
                    or args.get("start_time")
                    or args.get("start")
                    or ""
                ).strip()
                query_end = str(
                    args.get("query_end")
                    or args.get("end_time")
                    or args.get("end")
                    or ""
                ).strip()
                time_filter_mode = str(args.get("time_filter_mode") or "overlap").strip()

                tool_out = vector_tool.search(
                    pattern=pattern,
                    max_results=max_results,
                    query_subject=query_subject,
                    query_object=query_object,
                    query_predicate=query_predicate,
                    query_start=query_start,
                    query_end=query_end,
                    time_filter_mode=time_filter_mode,
                )

                trace.append(
                    {
                        "role": "tool_call",
                        "tool_name": tool_name,
                        "args": {
                            "pattern": pattern,
                            "max_results": max_results,
                            "query_subject": query_subject,
                            "query_object": query_object,
                            "query_predicate": query_predicate,
                            "query_start": query_start,
                            "query_end": query_end,
                            "time_filter_mode": time_filter_mode,
                        },
                    }
                )
                trace.append({"role": "tool_result", "content": tool_out})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_out, ensure_ascii=False),
                    }
                )
            continue

        last_response = msg.content or ""
        trace.append({"role": "assistant", "content": last_response})

        extracted = base.extract_answer(last_response)
        if extracted:
            if base.is_valid_for_expected_format(extracted, expected["name"]):
                termination_reason = "answer_tag_found"
                break

            format_retry_count += 1
            trace.append(
                {
                    "role": "format_check",
                    "expected_format": expected["name"],
                    "valid": False,
                    "attempt": format_retry_count,
                    "answer": extracted,
                }
            )
            if format_retry_count > max(0, int(format_max_retries)):
                termination_reason = "answer_tag_found_invalid_format_max_retries"
                break

            assistant_echo: Dict[str, Any] = {"role": "assistant", "content": last_response}
            if reasoning_content is not None:
                assistant_echo["reasoning_content"] = reasoning_content
            messages.append(assistant_echo)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous <answer> format is invalid for this question wording. "
                        f"Fix format only. {expected['instruction']} "
                        "Do not add extra text outside <answer>...</answer>."
                    ),
                }
            )
            trace.append({"role": "continue", "reason": "invalid answer format"})
            continue

        assistant_echo: Dict[str, Any] = {"role": "assistant", "content": last_response}
        if reasoning_content is not None:
            assistant_echo["reasoning_content"] = reasoning_content
        messages.append(assistant_echo)
        messages.append({"role": "system", "content": continue_hint})
        trace.append({"role": "continue", "reason": "no <answer> tag yet"})

    final_answer = base.extract_answer(last_response)
    postprocess: Dict[str, Any] = {}

    return {
        "raw_answer": last_response,
        "answer": final_answer,
        "tool_calls": tool_calls_count,
        "termination_reason": termination_reason,
        "expected_format": expected["name"],
        "postprocess": postprocess,
        "trace": trace,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate deepseek-chat + vector retrieval tool on TimelineKGQA."
    )
    parser.add_argument(
        "--questions-file",
        type=Path,
        default=Path("Datasets/unified_kg_icews_actor_questions_all.csv"),
    )
    parser.add_argument(
        "--kg-file",
        type=Path,
        default=Path("Datasets/unified_kg_icews_actor.csv"),
    )
    parser.add_argument(
        "--emb-dir",
        type=Path,
        default=Path("embeddings/unified_kg_icews_actor_glm3_raw"),
    )
    parser.add_argument("--num", type=int, default=200)
    parser.add_argument("--model", type=str, default="deepseek-chat")
    parser.add_argument("--base-url", type=str, default="https://api.deepseek.com")
    parser.add_argument("--chat-api-key", type=str, default=None)
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("prompts/grep_eval_prompt.json"),
    )
    parser.add_argument("--embed-model", type=str, default="embedding-3")
    parser.add_argument("--embed-base-url", type=str, default="https://open.bigmodel.cn/api/paas/v4")
    parser.add_argument("--embed-api-key", type=str, default=None)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--format-max-retries", type=int, default=2)
    parser.add_argument("--tool-max-results", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--random-sample", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-name", type=str, default="deepseek_chat_vector_icews_actor_rand200")
    parser.add_argument(
        "--disable-entity-filter",
        action="store_true",
        help="Disable query_subject/query_object filtering in the vector tool.",
    )
    parser.add_argument(
        "--disable-relation-filter",
        action="store_true",
        help="Disable query_predicate filtering in the vector tool.",
    )
    args = parser.parse_args()

    base.load_env(Path(".env"))
    base.load_env(Path("..") / ".env")

    chat_key = args.chat_api_key or base.first_env(
        ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "API_KEY", "api_key"]
    )
    if not chat_key:
        raise SystemExit("Missing chat API key. Set DEEPSEEK_API_KEY or OPENAI_API_KEY in env/.env.")
    embed_key = args.embed_api_key or base.first_env(
        ["ZHIPU_API_KEY", "GLM_API_KEY", "API_KEY", "api_key"]
    )
    if not embed_key:
        raise SystemExit("Missing embedding key. Set ZHIPU_API_KEY/GLM_API_KEY in env/.env.")

    if not args.questions_file.exists():
        raise SystemExit(f"Questions file not found: {args.questions_file}")
    if not args.kg_file.exists():
        raise SystemExit(f"KG file not found: {args.kg_file}")
    if not args.emb_dir.exists():
        raise SystemExit(f"Embedding dir not found: {args.emb_dir}")

    base_url = base.normalize_base_url(args.base_url)
    prompt_config = base.load_prompt_config(args.prompt_file)
    questions = base.load_test_questions(
        args.questions_file,
        args.num,
        random_sample=args.random_sample,
        seed=args.seed,
    )
    if not questions:
        raise SystemExit("No test questions loaded.")

    vector_tool = VectorTool(
        emb_dir=args.emb_dir,
        embed_api_key=embed_key,
        embed_base_url=args.embed_base_url,
        embed_model=args.embed_model,
    )

    out_dir = Path("eval_runs")
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    total = len(questions)
    results: List[Dict[str, Any] | None] = [None] * total
    exact_hits = 0
    exact_ci_hits = 0
    substring_hits = 0
    completed = 0
    lock = Lock()

    def worker(idx: int, item: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        local_client = OpenAI(api_key=chat_key, base_url=base_url)
        run = run_one_question(
            local_client,
            vector_tool,
            model=args.model,
            kg_file=args.kg_file,
            question=item["question"],
            question_type=item["question_type"],
            answer_type=item["answer_type"],
            prompt_config=prompt_config,
            max_steps=args.max_steps,
            tool_max_results=args.tool_max_results,
            format_max_retries=args.format_max_retries,
            disable_entity_filter=bool(args.disable_entity_filter),
            disable_relation_filter=bool(args.disable_relation_filter),
        )
        score = base.score_prediction(run["answer"], item["gold"])
        result_row: Dict[str, Any] = {
            "id": item["id"],
            "question": item["question"],
            "gold": item["gold"],
            "question_type": item["question_type"],
            "answer_type": item["answer_type"],
            "pred": run["answer"],
            "raw_answer": run["raw_answer"],
            "tool_calls": run["tool_calls"],
            "termination_reason": run.get("termination_reason", ""),
            "expected_format": run.get("expected_format", ""),
            "postprocess": run.get("postprocess", {}),
            **score,
            "trace": run["trace"],
        }
        return idx, result_row

    max_workers = max(1, int(args.concurrency))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_meta = {
            ex.submit(worker, idx, item): (idx, item) for idx, item in enumerate(questions)
        }
        for fut in as_completed(future_to_meta):
            qidx, qitem = future_to_meta[fut]
            try:
                idx, row = fut.result()
            except Exception as err:
                idx = qidx
                row = {
                    "id": qitem.get("id", ""),
                    "question": qitem.get("question", ""),
                    "gold": qitem.get("gold", ""),
                    "question_type": qitem.get("question_type", ""),
                    "answer_type": qitem.get("answer_type", ""),
                    "pred": "",
                    "raw_answer": "",
                    "tool_calls": 0,
                    "termination_reason": "error",
                    "exact": False,
                    "exact_ci": False,
                    "substring_ci": False,
                    "trace": [{"role": "error", "content": str(err)}],
                    "error": str(err),
                }

            with lock:
                results[idx] = row
                exact_hits += int(bool(row.get("exact")))
                exact_ci_hits += int(bool(row.get("exact_ci")))
                substring_hits += int(bool(row.get("substring_ci")))
                completed += 1
                print(
                    f"[{completed}/{total}] "
                    f"exact={exact_hits}/{completed} ({exact_hits / completed:.3f}) | "
                    f"exact_ci={exact_ci_hits}/{completed} ({exact_ci_hits / completed:.3f}) | "
                    f"tool_calls={row.get('tool_calls', 0)}"
                )

    duration = time.time() - start
    final_results = [r for r in results if r is not None]
    summary = {
        "num_questions": total,
        "model": args.model,
        "prompt_file": str(args.prompt_file),
        "disable_entity_filter": bool(args.disable_entity_filter),
        "disable_relation_filter": bool(args.disable_relation_filter),
        "concurrency": max_workers,
        "random_sample": bool(args.random_sample),
        "seed": int(args.seed),
        "kg_file": str(args.kg_file),
        "questions_file": str(args.questions_file),
        "emb_dir": str(args.emb_dir),
        "embed_model": args.embed_model,
        "metrics": {
            "exact_match": exact_hits / max(1, total),
            "exact_match_case_insensitive": exact_ci_hits / max(1, total),
            "substring_case_insensitive": substring_hits / max(1, total),
            "exact_hits": exact_hits,
            "exact_ci_hits": exact_ci_hits,
            "substring_ci_hits": substring_hits,
        },
        "duration_sec": duration,
        "results": final_results,
    }

    out_path = out_dir / f"{args.out_name}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {out_path}")
    print(
        f"Final: exact={exact_hits/max(1, total):.3f}, "
        f"exact_ci={exact_ci_hits/max(1, total):.3f}, "
        f"substring_ci={substring_hits/max(1, total):.3f}, "
        f"time={duration:.1f}s"
    )


if __name__ == "__main__":
    main()
