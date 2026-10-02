# Eval report 20261002T045107Z

**Verdict: PASS**

git `cfb3aee+dirty` · mode `replay` · nights [0, 1, 2, 3, 4] · prompts group=`group-v1`, draft=`draft-v1`, shorten=`shorten-v1`, judge=`judge-v1` · judge `google/gemma-4-31b` · limit 3000 chars

**Warnings**

- judge model google/gemma-4-31b is also under test (gemma-31b): scores for its own messages may be biased; set a different llm.steps.judge for a fair comparison
- judge not yet validated against human labels (EV-13): no labelled set

## Hard gates (must be 100%)

| Gate | Passed | Failed |
|---|---|---|
| EV-1 | 5 | 0 |
| EV-2 | 3 | 0 |
| EV-3 | 1 | 0 |
| EV-4 | 1 | 0 |
| EV-5 | 1 | 0 |
| EV-6 | 1 | 0 |
| EV-7 | 4 | 0 |
| EV-8 | 2 | 0 |
| EV-9 | 2 | 0 |
| EV-10 | 1 | 0 |
| EV-17 | 1 | 0 |
| run behaviour | 3 | 0 |

`25 passed in 43.66s`

## Models side by side (EV-16)

| Metric | qwen-27b (`qwen/qwen3.8-27b`) | gemma-31b (`google/gemma-4-31b`) |
|---|---|---|
| EV-3 posted messages failing faithfulness | 0 | 0 |
| LLM-written summaries % | 80 | 100 |
| fallback % | 20 | 0 |
| fallback reasons | chain_failed | – |
| EV-11 group: first-attempt pass % | 20 | 100 |
| EV-11 group: pass after retries % | 80 | 100 |
| EV-11 group: mean attempts | 2 | 1 |
| EV-11 group: mean latency s | 108.1 | 73.4 |
| EV-11 draft: first-attempt pass % | 75 | 80 |
| EV-11 draft: pass after retries % | 100 | 80 |
| EV-11 draft: mean attempts | 1.25 | 1 |
| EV-11 draft: mean latency s | 82.4 | 100.9 |
| EV-11 shorten: first-attempt pass % | – | 100 |
| EV-11 shorten: pass after retries % | – | 100 |
| EV-11 shorten: mean attempts | – | 1 |
| EV-11 shorten: mean latency s | – | 101.5 |
| EV-14 length min / median / max | 1218 / 2482 / 2704 | 2343 / 2564 / 2915 |
| EV-14 over limit | 0 | 0 |
| EV-12 judge most_important_first | 4.75 | 3.6 |
| EV-12 judge understandable_in_30s | 3.75 | 3.8 |
| EV-12 judge themes_sensible | 4.75 | 5 |
| EV-12 judge tone_neutral | 5 | 5 |
| EV-12 judge next_actions_clear | 5 | 4.4 |
| EV-12 judge overall | 4.5 | 4.2 |
| EV-12 judge overall — fallback baseline | 3.8 | 3.8 |
| cassette hits / misses | 15 / 0 | 11 / 0 |

_Judge scores are reported alongside, never instead of, the hard gates._

## Per night

### qwen-27b

| Night | Source | Length | Faithful | Attempts (group/draft/shorten) | Judge |
|---|---|---|---|---|---|
| 0 (2026-10-01) | fallback (chain_failed) | 1218 | yes | 3/–/– | – |
| 1 (2026-10-02) | llm | 2704 | yes | 2/1/– | 3 |
| 2 (2026-10-03) | llm | 2365 | yes | 2/1/– | 5 |
| 3 (2026-10-05) | llm | 2663 | yes | 2/1/– | 5 |
| 4 (2026-10-08) | llm | 2482 | yes | 1/2/– | 5 |

### gemma-31b

| Night | Source | Length | Faithful | Attempts (group/draft/shorten) | Judge |
|---|---|---|---|---|---|
| 0 (2026-10-01) | llm | 2915 | yes | 1/1/1 | 3 |
| 1 (2026-10-02) | llm | 2564 | yes | 1/1/– | 4 |
| 2 (2026-10-03) | llm | 2696 | yes | 1/1/– | 5 |
| 3 (2026-10-05) | llm | 2370 | yes | 1/1/– | 5 |
| 4 (2026-10-08) | llm | 2343 | yes | 1/1/– | 4 |

## Judge vs human (EV-13)

Status: no labelled set

