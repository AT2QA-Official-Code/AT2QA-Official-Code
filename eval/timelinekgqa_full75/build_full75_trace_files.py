"""Build single-file full-trace JSON artifacts for the 0.75+ CRON and ICEWS runs.

Outputs:
  - TimelineKGQA/eval_runs/cron_full75_complete_traces.json
  - TimelineKGQA/eval_runs/icews_full75_complete_traces.json

CRON:
  Stitches the previously-run v17 files by question id with "last file wins",
  then writes rows in official test-set order.

ICEWS:
  Concatenates the noleak replay batch files in batch order into one JSON file.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
EVAL_RUNS = ROOT / "eval_runs"

CRON_SOURCE_FILES = [
    "deepseek_chat_vector_cron_noleak_rand500_seed20260315_prompt_v17_v13plus_daysdict_whononhuman_c30.json",
    "deepseek_chat_vector_cron_noleak_rand1000_seed20260322_prompt_v17_c100.json",
    "deepseek_chat_vector_cron_noleak_rand300_seed20260316_prompt_v17_c100.json",
    "deepseek_chat_vector_cron_noleak_rand300_seed20260317_prompt_v17_c100.json",
    "deepseek_chat_vector_cron_noleak_rand300_seed20260317_prompt_v17_c100_rerun2.json",
    "deepseek_chat_vector_cron_noleak_rand300_seed20260317_prompt_v17_c100_rerun3.json",
    "deepseek_chat_vector_cron_noleak_rand300_seed20260321_prompt_v17_c100.json",
    "deepseek_chat_vector_cron_noleak_remaining6196_prompt_v17_c100.json",
]

ICEWS_NOLEAK_SUMMARY = (
    EVAL_RUNS
    / "noleak_replay_icews"
    / "deepseek_reasoner_vector_icews_all14621_noleak_replay_summary.json"
)


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_test_ids(csv_path: Path) -> List[str]:
    ids: List[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("split") or "").strip().lower() != "test":
                continue
            ids.append((row.get("id") or "").strip())
    return ids


def _metrics_from_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    exact_hits = 0
    exact_ci_hits = 0
    substring_ci_hits = 0
    total = 0
    for row in rows:
        total += 1
        exact_hits += int(bool(row.get("exact")))
        exact_ci_hits += int(bool(row.get("exact_ci")))
        substring_ci_hits += int(bool(row.get("substring_ci")))
    return {
        "exact_match": exact_hits / max(1, total),
        "exact_match_case_insensitive": exact_ci_hits / max(1, total),
        "substring_case_insensitive": substring_ci_hits / max(1, total),
        "exact_hits": exact_hits,
        "exact_ci_hits": exact_ci_hits,
        "substring_ci_hits": substring_ci_hits,
    }


def _write_json_with_results(
    output_path: Path,
    header: Dict[str, Any],
    rows: Iterable[Dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        f.write("{\n")
        header_items = list(header.items())
        for idx, (key, value) in enumerate(header_items):
            f.write(f'  "{key}": ')
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write(",\n")
        f.write('  "results": [\n')

        first = True
        count = 0
        for row in rows:
            if not first:
                f.write(",\n")
            first = False
            dumped = json.dumps(row, ensure_ascii=False, indent=2)
            indented = "\n".join("    " + line for line in dumped.splitlines())
            f.write(indented)
            count += 1

        f.write("\n  ]\n")
        f.write("}\n")
    print(f"Saved {count} rows to {output_path}")


def build_cron_trace_file() -> Path:
    by_id: Dict[str, Dict[str, Any]] = {}
    prompt_files: List[str] = []
    model = ""

    for name in CRON_SOURCE_FILES:
        path = EVAL_RUNS / name
        payload = _load_json(path)
        model = model or str(payload.get("model", ""))
        prompt = str(payload.get("prompt_file", ""))
        if prompt and prompt not in prompt_files:
            prompt_files.append(prompt)
        for row in payload.get("results", []):
            row_id = str(row.get("id", "")).strip()
            if row_id:
                by_id[row_id] = row

    test_ids = _load_test_ids(ROOT / "Datasets" / "unified_kg_cron_questions_all.csv")
    missing = [row_id for row_id in test_ids if row_id not in by_id]
    if missing:
        raise RuntimeError(f"CRON missing stitched ids: {missing[:20]}")

    ordered_rows = [by_id[row_id] for row_id in test_ids]
    header = {
        "dataset": "cron",
        "run_type": "full_test_stitched_with_traces",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "prompt_files": prompt_files,
        "source_files_ordered": [str(EVAL_RUNS / name) for name in CRON_SOURCE_FILES],
        "stitch_policy": "last_file_wins_by_id",
        "num_questions_total": len(ordered_rows),
        "metrics": _metrics_from_rows(ordered_rows),
    }

    output_path = EVAL_RUNS / "cron_full75_complete_traces.json"
    _write_json_with_results(output_path, header, ordered_rows)
    return output_path


def _iter_icews_rows() -> Iterable[Dict[str, Any]]:
    summary = _load_json(ICEWS_NOLEAK_SUMMARY)
    seen_ids = set()
    for item in summary.get("per_file", []):
        output_file = item.get("output_file")
        if not output_file:
            continue
        path = ICEWS_NOLEAK_SUMMARY.parent / str(output_file)
        payload = _load_json(path)
        for row in payload.get("results", []):
            row_id = str(row.get("id", "")).strip()
            if row_id in seen_ids:
                raise RuntimeError(f"ICEWS duplicate id while stitching: {row_id}")
            if row_id:
                seen_ids.add(row_id)
            yield row


def build_icews_trace_file() -> Path:
    summary = _load_json(ICEWS_NOLEAK_SUMMARY)
    test_ids = _load_test_ids(ROOT / "Datasets" / "unified_kg_icews_actor_questions_all.csv")
    expected_total = len(test_ids)
    reported_total = int(summary.get("num_rows_total", 0))
    if expected_total != reported_total:
        raise RuntimeError(
            f"ICEWS total mismatch: dataset has {expected_total}, summary reports {reported_total}"
        )

    header = {
        "dataset": "icews_actor",
        "run_type": "full_test_noleak_replay_with_traces",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_summary_file": str(ICEWS_NOLEAK_SUMMARY),
        "num_questions_total": reported_total,
        "metrics": summary.get("all_metrics", {}),
        "noleak_adjustment_stats": {
            "changed_pred_rows_total": summary.get("changed_pred_rows_total"),
            "removed_symbolic_trace_steps_total": summary.get("removed_symbolic_trace_steps_total"),
        },
        "source_files_ordered": [
            str(ICEWS_NOLEAK_SUMMARY.parent / str(item.get("output_file")))
            for item in summary.get("per_file", [])
        ],
    }

    output_path = EVAL_RUNS / "icews_full75_complete_traces.json"
    _write_json_with_results(output_path, header, _iter_icews_rows())
    return output_path


def main() -> None:
    cron_path = build_cron_trace_file()
    icews_path = build_icews_trace_file()
    print(f"Built: {cron_path}")
    print(f"Built: {icews_path}")


if __name__ == "__main__":
    main()
