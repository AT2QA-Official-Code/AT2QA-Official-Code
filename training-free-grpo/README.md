# Training-Free GRPO (Prompt Selection)

This folder implements a training-free few-shot selection pipeline consistent with your paper description.

## What it does

Given:
- batch size `n`
- group size `g`
- memory size `k`

Pipeline:
1. For each of `n` sampled train questions, generate `g` trajectories (`n*g` total).
2. Compute which trajectories are correct (`correct=True`).
3. For each question, run **LLM-guided group computation** on correct trajectories to extract high-value advantage texts.
4. Validate each advantage text on a dev subset:
   - compare `base prompt` vs `base prompt + this one advantage text`
   - compute validation gain (`delta_vs_baseline`).
5. Select top-`k` advantage texts by gain and update `experience_bank.json`.

## Run

From `paper_release_clean` root:

```bash
python training-free-grpo/run_training_free_grpo.py \
  --base-url https://api.deepseek.com \
  --chat-model deepseek-chat \
  --n 32 \
  --g 4 \
  --k 8 \
  --val-sample 300 \
  --concurrency 12
```

## Inputs

- Train questions: `MultiTQ/questions/train.json`
- Dev questions: `MultiTQ/questions/dev.json`
- KG: `MultiTQ/kg/full.txt`
- Embeddings: `MultiTQ/embeddings/full_norm256_conc/*`
- Base prompt: `eval/prompts/system.md` (zero-shot)

## Outputs

- `training-free-grpo/runs/stage1_candidate_runs.json`
- `training-free-grpo/runs/stage2_group_advantages.json`
- `training-free-grpo/runs/stage3_validation_scores.json`
- `training-free-grpo/runs/report.json`
- updated `training-free-grpo/experience_bank.json`

## Notes

- This pipeline is **training-free**: no gradient updates, only rule/prompt editing by validation feedback.
- Standard evaluation script (`eval/agent_eval.py`) defaults to few-shot prompt.
- pass@k script (`scripts/run_passk_zero_shot.py`) defaults to zero-shot prompt.

