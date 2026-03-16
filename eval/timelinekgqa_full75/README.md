# TimelineKGQA Full-Trace Release (Cron + ICEWS)

This directory stores the exact code snapshots and prompt files used for the
released full-trace artifacts corresponding to:

- `Cron`: `6295 / 8344 = 0.7544343240651965`
- `ICEWS`: `11024 / 14621 = 0.7539839956227344`

The full trace files are stored under:

- `../../full_sampled_trajectories/timelinekgqa_full75/cron_full75_complete_traces.json`
- `../../full_sampled_trajectories/timelinekgqa_full75/icews_full75_complete_traces.json`

## Included code snapshots

- `eval_grep_agent.py`
- `eval_vector_agent_cron_noleak.py`
- `eval_vector_agent_icews.py`
- `build_full75_trace_files.py`
- `prompts/grep_eval_prompt_v17_chat_v13plus_daysdict_whononhuman.json`
- `prompts/grep_eval_prompt_or_union_hint_time_fewshot.json`

## Reproduction commands

These were run in the original `TimelineKGQA/` working tree with the matching
datasets, embeddings, and saved run JSONs. Do not hardcode API keys; provide
them through environment variables or command flags.

### 1. CRON end-to-end agent run

```bash
cd TimelineKGQA
python eval_vector_agent_cron_noleak.py \
  --model deepseek-chat \
  --prompt-file prompts/grep_eval_prompt_v17_chat_v13plus_daysdict_whononhuman.json \
  --questions-file Datasets/unified_kg_cron_questions_all.csv \
  --kg-file Datasets/unified_kg_cron.csv \
  --emb-dir embeddings/unified_kg_cron_glm3_raw \
  --num 8344 \
  --concurrency 100 \
  --disable-entity-filter \
  --disable-relation-filter \
  --out-name deepseek_chat_vector_cron_noleak_alltest8344_prompt_v17_c100
```

Note: the released `Cron` full-trace artifact at `0.7544343240651965` is the
stitched full-test file built from the saved v17 source runs, not a single
fresh online all-test run.

### 2. ICEWS end-to-end agent run

```bash
cd TimelineKGQA
python eval_vector_agent_icews.py \
  --model deepseek-reasoner \
  --prompt-file prompts/grep_eval_prompt_or_union_hint_time_fewshot.json \
  --questions-file Datasets/unified_kg_icews_actor_questions_all.csv \
  --kg-file Datasets/unified_kg_icews_actor.csv \
  --emb-dir embeddings/unified_kg_icews_actor_glm3_raw \
  --num 14621 \
  --concurrency 100 \
  --disable-entity-filter \
  --disable-relation-filter \
  --out-name deepseek_reasoner_vector_icews_alltest14621_timeonly_fewshot
```

### 3. Offline exact full-trace reconstruction from saved run JSONs

```bash
cd TimelineKGQA
python script/build_full75_trace_files.py
```

This command reconstructs the exact released full-trace files from the saved
run outputs already present under `TimelineKGQA/eval_runs/`.
