from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
import os
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TUPLE_RE = re.compile(r"^\(\s*[^,]+,\s*[^)]+\)$")

TRTCR_QTYPE = "timeline_recovery_temporal_constrainted_retrieval"

# Allen relation lookup (same relation codes as generator.py).
ALLEN_RELATION_BY_SIG: Dict[Tuple[int, int, int, int, int, int], str] = {
    (-1, -1, -1, -1, -1, -1): "X < Y",
    (-1, -1, -1, -1, 0, -1): "X m Y",
    (-1, -1, -1, -1, 1, -1): "X o Y",
    (-1, -1, -1, -1, 1, 0): "X fi Y",
    (-1, -1, -1, -1, 1, 1): "X di Y",
    (-1, -1, 0, -1, 1, -1): "X s Y",
    (-1, -1, 0, -1, 1, 0): "X = Y",
    (-1, -1, 0, -1, 1, 1): "X si Y",
    (-1, -1, 1, -1, 1, -1): "X d Y",
    (-1, -1, 1, -1, 1, 0): "X f Y",
    (-1, -1, 1, -1, 1, 1): "X oi Y",
    (-1, -1, 1, 0, 1, 1): "X mi Y",
    (-1, -1, 1, 1, 1, 1): "X > Y",
    (0, -1, -1, -1, -1, -1): "X < Y",
    (0, -1, 0, -1, 0, -1): "X s Y",
    (0, -1, 1, -1, 1, -1): "X d Y",
    (0, -1, 1, 0, 1, 0): "X f Y",
    (0, -1, 1, 1, 1, 1): "X > Y",
    (-1, 0, -1, -1, -1, -1): "X < Y",
    (-1, 0, -1, -1, 0, 0): "X fi Y",
    (-1, 0, -1, -1, 1, 1): "X di Y",
    (-1, 0, 0, 0, 1, 1): "X si Y",
    (-1, 0, 1, 1, 1, 1): "X > Y",
    (0, 0, -1, -1, -1, -1): "X < Y",
    (0, 0, 0, 0, 0, 0): "X = Y",
    (0, 0, 1, 1, 1, 1): "X > Y",
}

_KG_INDEX_CACHE: Dict[str, Dict[str, Any]] = {}
_KG_INDEX_LOCK = Lock()


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def first_env(keys: List[str]) -> str | None:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return None


def normalize_base_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if not u.endswith("/v1"):
        u = f"{u}/v1"
    return u


def load_test_questions(
    path: Path, num: int, *, random_sample: bool = False, seed: int = 42
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("split") or "").strip().lower() != "test":
                continue
            rows.append(
                {
                    "id": (row.get("id") or "").strip(),
                    "question": (row.get("question") or "").strip(),
                    "gold": (row.get("answer") or "").strip(),
                    "question_type": (row.get("question_type") or "").strip(),
                    "answer_type": (row.get("answer_type") or "").strip(),
                }
            )
    if random_sample:
        rng = random.Random(seed)
        if len(rows) <= num:
            return rows
        return rng.sample(rows, k=num)
    return rows[:num]


def parse_kg_csv_line(raw_line: str) -> Dict[str, str]:
    try:
        row = next(csv.reader([raw_line]))
    except Exception:
        return {"raw": raw_line[:300]}

    if len(row) < 9:
        return {"raw": raw_line[:300]}

    return {
        "id": row[0],
        "subject": row[1],
        "predicate": row[3],
        "object": row[5],
        "start_time": row[7],
        "end_time": row[8],
    }


def _date_to_ord(value: str) -> int:
    s = (value or "").strip()
    if s == "beginning of time":
        return date(1, 1, 1).toordinal()
    if s == "end of time":
        return date(9999, 12, 31).toordinal()
    if "T" in s:
        s = s.split("T", 1)[0]
    return datetime.strptime(s, "%Y-%m-%d").date().toordinal()


def _sign(x: int) -> int:
    if x == 0:
        return 0
    return -1 if x < 0 else 1


def _allen_relation_code(a_start: int, a_end: int, b_start: int, b_end: int) -> str | None:
    key = (
        _sign(a_start - a_end),
        _sign(b_start - b_end),
        _sign(a_start - b_start),
        _sign(a_start - b_end),
        _sign(a_end - b_start),
        _sign(a_end - b_end),
    )
    return ALLEN_RELATION_BY_SIG.get(key)


def _load_kg_index(kg_file: Path) -> Dict[str, Any]:
    cache_key = str(kg_file.resolve())
    with _KG_INDEX_LOCK:
        cached = _KG_INDEX_CACHE.get(cache_key)
        if cached is not None:
            return cached

    rows: List[Dict[str, Any]] = []
    by_subject: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_object: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

    with kg_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader, 1):
            if len(row) < 9:
                continue
            if (row[3] or "").strip() != "Affiliation To":
                continue
            subject = (row[1] or "").strip()
            obj = (row[5] or "").strip()
            start_time = (row[7] or "").strip()
            end_time = (row[8] or "").strip()
            if not subject or not obj or not start_time or not end_time:
                continue
            try:
                start_ord = _date_to_ord(start_time)
                end_ord = _date_to_ord(end_time)
            except Exception:
                continue
            record = {
                "line_no": line_no,
                "subject": subject,
                "object": obj,
                "start_time": start_time,
                "end_time": end_time,
                "start_ord": start_ord,
                "end_ord": end_ord,
            }
            rows.append(record)
            by_subject[subject].append(record)
            by_object[obj].append(record)
            by_pair[(subject, obj)].append(record)

    index = {
        "rows": rows,
        "by_subject": dict(by_subject),
        "by_object": dict(by_object),
        "by_pair": dict(by_pair),
    }
    with _KG_INDEX_LOCK:
        _KG_INDEX_CACHE[cache_key] = index
    return index


def _strip_relation_dict_prefix(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^\s*\{[^{}]*'relation'[^{}]*\}\s*", "", s)
    return s.strip()


def _strip_leading_connectors(text: str) -> str:
    s = _strip_relation_dict_prefix(text)
    low = s.casefold()
    connector_phrases = [
        "in the course of",
        "in the midst of",
        "at the same time",
        "in advance of",
        "earlier than",
        "early than",
        "prior to",
        "preceding",
        "ahead of",
        "before",
        "after",
        "during",
        "while",
        "when",
    ]
    last_hit = -1
    last_len = 0
    for phrase in connector_phrases:
        idx = low.rfind(phrase)
        if idx > last_hit:
            last_hit = idx
            last_len = len(phrase)
    if last_hit >= 0 and last_hit + last_len < len(s):
        s = s[last_hit + last_len :].strip(" ,")

    patterns = [
        r"^(?:before|after|during|while)\s+",
        r"^(?:in the course of|in the midst of|at the same time)\s+",
        r"^(?:early than|earlier than|prior to|ahead of|preceding|in advance of)\s+",
        r"^when\s+",
    ]
    for pat in patterns:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
    s = re.sub(
        r"\b(?:start|starts|finish|finishes|end|ends)\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    return s


def _extract_query_object_for_subject(question: str) -> str:
    q = norm_text(question)
    patterns = [
        r"(?i)\bwho(?:/which organisation)?(?:\s+starts|\s+finishes)?\s+affiliation to\s+(.+?)(?=(?:\s+\d+\s+days\b|\s+(?:before|after|during|while|in the course of|in the midst of|at the same time|ahead of|preceding|earlier than|in advance of)\b|,|\?|$))",
        r"(?i)\bwho\s+ends\s+affiliation to\s+(.+?)(?=(?:\s+when\b|,|\?|$))",
        r"(?i)\bwho\s+starts\s+affiliation to\s+(.+?)(?=(?:\s+when\b|,|\?|$))",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            return _strip_leading_connectors(m.group(1))
    return ""


def _extract_query_subject_for_object(question: str) -> str:
    q = norm_text(question)
    patterns = [
        r"(?i)\b(.+?)\s+affiliation to\s+which\s+organisation\b",
        r"(?i)\bwhich organisation is affiliation toed by\s+(.+?)(?=(?:\s+(?:before|after|during|while|in the course of|in the midst of|at the same time|when)\b|,|\?|$))",
        r"(?i)\bwhich organisation is affiliation toed\s+(.+?)(?=(?:\s+(?:during|while|in the course of)\b|,|\?|$))",
        r"(?i)\bin which organisation\s+(.+?)\s+(?:starts|finishes)\s+affiliation to\b",
        r"(?i)\bin which organisation\s+(.+?)\s+affiliation to\b",
        r"(?i)\bin which organisation,\s*(.+?)\s+affiliation to\b",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            return _strip_leading_connectors(m.group(1))
    return ""


def _extract_reference_event(question: str, answer_type: str, query_slot: str) -> Tuple[str, str]:
    q = norm_text(question)
    pairs: List[Tuple[str, str]] = []
    for m in re.finditer(r"([^,?]+?)\s+Affiliation To\s+([^,?]+)", q, flags=re.IGNORECASE):
        subject = _strip_leading_connectors(m.group(1))
        obj = _strip_leading_connectors(m.group(2))
        if not subject or not obj:
            continue
        sl = subject.casefold()
        if sl.startswith("who") or sl.startswith("which organisation") or sl.startswith("which organization"):
            continue
        if "which organisation" in obj.casefold() or "which organization" in obj.casefold():
            continue
        pairs.append((subject, obj))

    if not pairs:
        return "", ""

    if answer_type == "subject":
        # query slot = first-event object; avoid picking pseudo first-event from malformed captures
        for subject, obj in pairs:
            if obj != query_slot:
                return subject, obj
        return pairs[0]

    # answer_type == object; query slot = first-event subject
    for subject, obj in pairs:
        if subject.casefold() != query_slot.casefold():
            return subject, obj
    return pairs[0]


def _parse_relation_spec(question: str) -> Dict[str, Any]:
    q = norm_text(question)
    ql = q.casefold()

    relation_match = re.search(r"'relation'\s*:\s*'([^']+)'", q)
    if relation_match:
        return {"mode": "relation_set", "relations": {relation_match.group(1)}}

    days_match = re.search(r"(\d+)\s+days", ql)
    if days_match:
        days = int(days_match.group(1))
        if "after" in ql:
            return {"mode": "duration_after", "days": days}
        if "before" in ql:
            return {"mode": "duration_before", "days": days}

    if "same start and end time" in ql:
        return {"mode": "relation_set", "relations": {"X = Y"}}

    if (
        "when " in ql
        and " starts affiliation to" in ql
        and (" who ends affiliation to" in ql or " ends affiliation to with which" in ql)
    ):
        return {"mode": "relation_set", "relations": {"X m Y"}}

    if (
        "when " in ql
        and " ends affiliation to" in ql
        and (" who starts affiliation to" in ql or " starts affiliation to with which" in ql)
    ):
        return {"mode": "relation_set", "relations": {"X mi Y"}}

    if "at the same time" in ql and (" start affiliation to" in ql or " starts affiliation to" in ql):
        return {"mode": "relation_set", "relations": {"X s Y", "X si Y"}}

    if "at the same time" in ql and (" finish affiliation to" in ql or " finishes affiliation to" in ql):
        return {"mode": "relation_set", "relations": {"X f Y", "X fi Y"}}

    if re.search(
        r"\bbefore\b|prior to|earlier than|in advance of|ahead of|preceding|early than",
        ql,
    ):
        return {"mode": "relation_set", "relations": {"X < Y"}}

    if re.search(r"\bafter\b", ql):
        return {"mode": "relation_set", "relations": {"X > Y"}}

    if re.search(r"\bduring\b|\bwhile\b|in the course of|in the midst of", ql):
        return {"mode": "relation_set", "relations": {"X o Y", "X oi Y", "X d Y", "X di Y"}}

    if "at the same time" in ql:
        return {
            "mode": "relation_set",
            "relations": {"X o Y", "X oi Y", "X d Y", "X di Y", "X = Y"},
        }

    return {"mode": "unknown"}


def _collect_candidate_values_from_trace(trace: List[Dict[str, Any]], answer_type: str) -> List[str]:
    key = "subject" if answer_type == "subject" else "object"
    seen: List[str] = []
    seen_set: Set[str] = set()
    for step in trace:
        if step.get("role") != "tool_result":
            continue
        content = step.get("content", {}) or {}
        matches = content.get("matches", []) or []
        for m in matches:
            value = str(m.get(key, "")).strip()
            if not value or value in seen_set:
                continue
            seen.append(value)
            seen_set.add(value)
    return seen


def _symbolic_solve_trtcr(
    *,
    question: str,
    answer_type: str,
    kg_file: Path,
) -> Dict[str, Any]:
    if answer_type not in {"subject", "object"}:
        return {"ok": False, "reason": "unsupported_answer_type"}

    relation_spec = _parse_relation_spec(question)
    if relation_spec.get("mode") == "unknown":
        return {"ok": False, "reason": "unknown_relation"}

    if answer_type == "subject":
        query_slot = _extract_query_object_for_subject(question)
    else:
        query_slot = _extract_query_subject_for_object(question)
    if not query_slot:
        return {"ok": False, "reason": "query_slot_parse_failed"}

    ref_subject, ref_object = _extract_reference_event(question, answer_type, query_slot)
    if not ref_subject or not ref_object:
        return {"ok": False, "reason": "reference_event_parse_failed"}

    index = _load_kg_index(kg_file)
    by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = index["by_pair"]
    ref_events = by_pair.get((ref_subject, ref_object), [])
    if not ref_events:
        return {
            "ok": False,
            "reason": "reference_event_not_found_in_kg",
            "query_slot": query_slot,
            "reference_event": {"subject": ref_subject, "object": ref_object},
        }

    if answer_type == "subject":
        pool = index["by_object"].get(query_slot, [])
        value_key = "subject"
    else:
        pool = index["by_subject"].get(query_slot, [])
        value_key = "object"

    candidates_with_line: Dict[str, int] = {}
    mode = relation_spec["mode"]
    for candidate in pool:
        a_start = int(candidate["start_ord"])
        a_end = int(candidate["end_ord"])
        hit = False
        for ref in ref_events:
            b_start = int(ref["start_ord"])
            b_end = int(ref["end_ord"])

            if mode == "duration_before":
                if (b_start - a_end) == int(relation_spec["days"]):
                    hit = True
            elif mode == "duration_after":
                if (a_start - b_end) == int(relation_spec["days"]):
                    hit = True
            else:
                relation = _allen_relation_code(a_start, a_end, b_start, b_end)
                if relation in relation_spec.get("relations", set()):
                    hit = True
            if hit:
                break

        if hit:
            value = str(candidate[value_key]).strip()
            if not value:
                continue
            prev = candidates_with_line.get(value)
            line_no = int(candidate["line_no"])
            if prev is None or line_no < prev:
                candidates_with_line[value] = line_no

    sorted_candidates = [
        kv[0] for kv in sorted(candidates_with_line.items(), key=lambda item: item[1])
    ]
    return {
        "ok": True,
        "mode": mode,
        "relations": sorted(list(relation_spec.get("relations", set()))),
        "days": relation_spec.get("days"),
        "query_slot": query_slot,
        "reference_event": {"subject": ref_subject, "object": ref_object},
        "candidate_count": len(sorted_candidates),
        "candidates": sorted_candidates,
    }


def _postprocess_trtcr_answer(
    *,
    question: str,
    answer_type: str,
    kg_file: Path,
    llm_answer: str,
    trace: List[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    symbolic = _symbolic_solve_trtcr(
        question=question,
        answer_type=answer_type,
        kg_file=kg_file,
    )
    if not symbolic.get("ok"):
        return llm_answer, {"symbolic": symbolic, "decision": "keep_llm_parse_failed"}

    candidates: List[str] = list(symbolic.get("candidates", []))
    if not candidates:
        if llm_answer:
            return llm_answer, {"symbolic": symbolic, "decision": "keep_llm_no_candidates"}
        return "No Answer", {"symbolic": symbolic, "decision": "emit_no_answer_no_candidates"}

    llm = (llm_answer or "").strip()
    if llm in candidates:
        return llm, {"symbolic": symbolic, "decision": "keep_llm_in_candidates"}

    # Guard against false No Answer: if candidates exist, never output No Answer.
    seen_order = _collect_candidate_values_from_trace(trace, answer_type=answer_type)
    for value in seen_order:
        if value in candidates:
            return value, {"symbolic": symbolic, "decision": "override_with_trace_order_candidate"}

    # Structured fallback from full KG index order.
    return candidates[0], {"symbolic": symbolic, "decision": "override_with_first_structured_candidate"}


def grep_kg(
    kg_file: Path,
    pattern: str,
    *,
    fixed_string: bool = True,
    ignore_case: bool = True,
    max_results: int = 30,
) -> Dict[str, Any]:
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "empty pattern", "matches": []}

    cap = min(max(1, int(max_results)), 80)
    cmd = [
        "rg",
        "--no-heading",
        "--line-number",
        "--max-count",
        str(cap),
    ]
    if fixed_string:
        cmd.append("--fixed-strings")
    if ignore_case:
        cmd.append("--ignore-case")
    cmd += ["--", pattern, str(kg_file)]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode not in (0, 1):
        return {"error": proc.stderr.strip() or "rg failed", "matches": []}

    matches: List[Dict[str, Any]] = []
    output = proc.stdout.strip()
    if not output:
        return {"pattern": pattern, "matches": []}

    for line in output.splitlines():
        if ":" not in line:
            continue
        line_no_str, raw = line.split(":", 1)
        try:
            line_no = int(line_no_str)
        except ValueError:
            line_no = -1
        parsed = parse_kg_csv_line(raw)
        matches.append({"line_no": line_no, **parsed})

    return {"pattern": pattern, "matches": matches}


def extract_answer(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""

    tagged = re.findall(r"<answer>(.*?)</answer>", s, flags=re.IGNORECASE | re.DOTALL)
    if not tagged:
        return ""
    s = tagged[-1].strip()
    s = re.sub(r"^\s*answer\s*[:：]\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.strip().strip("`").strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def norm_text(s: str) -> str:
    return " ".join((s or "").strip().split())


def infer_expected_format(question: str) -> Dict[str, str]:
    q = norm_text(question)
    ql = q.casefold()

    # Keep this classifier wording-based only (no question_type leakage).
    if q.startswith("From when to when did "):
        return {
            "name": "range_and",
            "instruction": "Use exactly: <answer>start_time and end_time</answer>.",
        }
    if q.startswith("From when to when,"):
        return {
            "name": "tuple_or_no_answer",
            "instruction": (
                "Use exactly one of: <answer>(start_time, end_time)</answer> or "
                "<answer>No Answer</answer>."
            ),
        }
    if "during what time" in ql:
        return {
            "name": "range_and",
            "instruction": "Use exactly: <answer>start_time and end_time</answer>.",
        }
    if ql.startswith("is the duration of "):
        return {
            "name": "duration_compare_label",
            "instruction": "Output one label only: <answer>longer</answer>, <answer>shorter</answer>, or <answer>equal</answer>.",
        }
    if " is ranking what " in ql or ql.startswith("which one is "):
        return {
            "name": "rank_number",
            "instruction": "Output rank number only, e.g. <answer>1</answer>.",
        }
    if "jointly when " in ql and ("what is the duration of " in ql):
        return {
            "name": "tuple_or_no_answer",
            "instruction": (
                "For this duration wording, output overlap range only: "
                "<answer>(start_time, end_time)</answer> or <answer>No Answer</answer>."
            ),
        }
    if ql.startswith("how long did ") or ql.startswith("what is the duration of "):
        return {
            "name": "duration_end_minus_start",
            "instruction": "Use exactly: <answer>end_time - start_time</answer>.",
        }
    if ql.startswith("when did ") or ql.startswith("at what time did "):
        return {
            "name": "single_time",
            "instruction": "Output a single time token only (YYYY-MM-DD, beginning of time, or end of time).",
        }
    if ql.startswith("who ") or " by who " in ql:
        return {
            "name": "single_entity",
            "instruction": "Output one entity name only.",
        }
    if "which organisation" in ql or "which organization" in ql:
        return {
            "name": "single_entity",
            "instruction": "Output one organization/entity name only.",
        }
    return {
        "name": "free",
        "instruction": "Output one concise final answer inside <answer>...</answer>.",
    }


def is_valid_for_expected_format(answer: str, expected_format: str) -> bool:
    a = norm_text(answer)
    if not a:
        return False

    if expected_format == "free":
        return True
    if expected_format == "range_and":
        return bool(re.fullmatch(r".+ and .+", a))
    if expected_format == "tuple_or_no_answer":
        return a == "No Answer" or bool(TUPLE_RE.fullmatch(a))
    if expected_format == "single_time":
        return (
            a in {"beginning of time", "end of time"}
            or bool(DATE_RE.fullmatch(a))
            or a in {"0001-01-01T00:00:00.000000", "9999-12-31T23:59:59.999999"}
        )
    if expected_format == "duration_end_minus_start":
        return bool(re.fullmatch(r".+ - .+", a))
    if expected_format == "duration_compare_label":
        return a in {"longer", "shorter", "equal"}
    if expected_format == "rank_number":
        return bool(re.fullmatch(r"\d+", a))
    if expected_format == "single_entity":
        # Keep entity validation permissive to avoid false rejection on names with punctuation.
        return "," not in a and ";" not in a and not TUPLE_RE.fullmatch(a)
    return True


def score_prediction(pred: str, gold: str) -> Dict[str, bool]:
    p = norm_text(pred)
    g = norm_text(gold)
    if not p or not g:
        return {"exact": False, "exact_ci": False, "substring_ci": False}

    exact = p == g
    exact_ci = p.casefold() == g.casefold()
    substring_ci = p.casefold() in g.casefold() or g.casefold() in p.casefold()
    return {"exact": exact, "exact_ci": exact_ci, "substring_ci": substring_ci}


def chat_with_retry(
    client: OpenAI,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
) -> Any:
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0,
            )
        except (APIConnectionError, APITimeoutError, RateLimitError, APIStatusError) as err:
            last_err = err
            if attempt == 4:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"chat completion failed: {last_err}")


def load_prompt_config(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise SystemExit(f"Prompt file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"Prompt file must be a JSON object: {path}")
    required = ["system_prompt", "user_prompt_template", "continue_prompt"]
    for key in required:
        if key not in raw or not isinstance(raw[key], str) or not raw[key].strip():
            raise SystemExit(f"Prompt file missing non-empty key: {key}")
    return {
        "system_prompt": raw["system_prompt"],
        "user_prompt_template": raw["user_prompt_template"],
        "continue_prompt": raw["continue_prompt"],
    }


def render_prompt_template(template: str, question: str, answer_type: str) -> str:
    return template.replace("{question}", question).replace("{answer_type}", answer_type)


def run_one_question(
    client: OpenAI,
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
) -> Dict[str, Any]:
    system_prompt = prompt_config["system_prompt"]
    expected = infer_expected_format(question)
    user_content = render_prompt_template(
        prompt_config["user_prompt_template"], question, answer_type
    )
    user_content = (
        f"{user_content}\n\n"
        f"Answer-format requirement (inferred from wording): {expected['instruction']}"
    )
    continue_hint = render_prompt_template(
        prompt_config["continue_prompt"], question, answer_type
    )

    tool_spec: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "grep_kg",
                "description": "Search rows in unified_kg_icews_actor.csv with ripgrep.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "The text or regex pattern to search in KG CSV.",
                        },
                        "fixed_string": {
                            "type": "boolean",
                            "description": "If true, treat pattern as plain text.",
                            "default": True,
                        },
                        "ignore_case": {
                            "type": "boolean",
                            "description": "If true, search case-insensitively.",
                            "default": True,
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum matched rows to return (1-80).",
                            "default": 30,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }
    ]

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": user_content,
        },
    ]
    trace: List[Dict[str, Any]] = [{"role": "user", "content": question}]
    tool_calls_count = 0
    last_response = ""
    termination_reason = "max_steps"
    format_retry_count = 0

    for _ in range(max_steps):
        resp = chat_with_retry(client, model=model, messages=messages, tools=tool_spec)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if tool_calls:
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [],
            }
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

                pattern = str(args.get("pattern", "")).strip()
                fixed_string = bool(args.get("fixed_string", True))
                ignore_case = bool(args.get("ignore_case", True))
                max_results = int(args.get("max_results", tool_max_results))
                if "max_results" not in args:
                    max_results = tool_max_results

                tool_out = grep_kg(
                    kg_file=kg_file,
                    pattern=pattern,
                    fixed_string=fixed_string,
                    ignore_case=ignore_case,
                    max_results=max_results,
                )

                trace.append(
                    {
                        "role": "tool_call",
                        "tool_name": "grep_kg",
                        "args": {
                            "pattern": pattern,
                            "fixed_string": fixed_string,
                            "ignore_case": ignore_case,
                            "max_results": max_results,
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

        extracted = extract_answer(last_response)
        if extracted:
            if is_valid_for_expected_format(extracted, expected["name"]):
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

            messages.append({"role": "assistant", "content": last_response})
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

        # No tag yet; continue the loop until max_steps or answer tag appears.
        messages.append({"role": "assistant", "content": last_response})
        messages.append(
            {
                "role": "system",
                "content": continue_hint,
            }
        )
        trace.append({"role": "continue", "reason": "no <answer> tag yet"})
        continue

    final_answer = extract_answer(last_response)
    postprocess: Dict[str, Any] = {}
    if (question_type or "").strip() == TRTCR_QTYPE:
        final_answer, postprocess = _postprocess_trtcr_answer(
            question=question,
            answer_type=answer_type,
            kg_file=kg_file,
            llm_answer=final_answer,
            trace=trace,
        )
        trace.append({"role": "symbolic_postprocess", "content": postprocess})

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
    parser = argparse.ArgumentParser(description="Evaluate deepseek-chat + grep tool on TimelineKGQA.")
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
    parser.add_argument("--num", type=int, default=100)
    parser.add_argument("--model", type=str, default="deepseek-chat")
    parser.add_argument("--base-url", type=str, default="https://api.deepseek.com")
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("prompts/grep_eval_prompt.json"),
    )
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--format-max-retries", type=int, default=2)
    parser.add_argument("--tool-max-results", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--random-sample", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-name", type=str, default="deepseek_chat_grep_icews_actor_first100_test")
    args = parser.parse_args()

    # load env from current and parent workspace root
    load_env(Path(".env"))
    load_env(Path("..") / ".env")

    api_key = first_env(["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "API_KEY", "api_key"])
    if not api_key:
        raise SystemExit("Missing API key. Set DEEPSEEK_API_KEY or OPENAI_API_KEY in env/.env.")

    if not args.questions_file.exists():
        raise SystemExit(f"Questions file not found: {args.questions_file}")
    if not args.kg_file.exists():
        raise SystemExit(f"KG file not found: {args.kg_file}")

    base_url = normalize_base_url(args.base_url)
    prompt_config = load_prompt_config(args.prompt_file)
    questions = load_test_questions(
        args.questions_file,
        args.num,
        random_sample=args.random_sample,
        seed=args.seed,
    )
    if not questions:
        raise SystemExit("No test questions loaded.")

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
        local_client = OpenAI(api_key=api_key, base_url=base_url)
        run = run_one_question(
            local_client,
            model=args.model,
            kg_file=args.kg_file,
            question=item["question"],
            question_type=item["question_type"],
            answer_type=item["answer_type"],
            prompt_config=prompt_config,
            max_steps=args.max_steps,
            tool_max_results=args.tool_max_results,
            format_max_retries=args.format_max_retries,
        )
        score = score_prediction(run["answer"], item["gold"])
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
        "concurrency": max_workers,
        "random_sample": bool(args.random_sample),
        "seed": int(args.seed),
        "kg_file": str(args.kg_file),
        "questions_file": str(args.questions_file),
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
