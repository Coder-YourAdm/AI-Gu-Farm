# B 运行清单与证据索引

> 2026-11-14 · 列出 `runs/` 下全部运行，区分「正式数据」与「开发/失败尝试」。
> 正式实验只有一套：协议 `7f878e7097d4…`（`runs/protocol_v5.json`），
> 代码版本 commit `d771ecbd74e6bf20401569909dc38fe306bb300b` / tag `b-formal-20261114`。

## 1 正式运行（只有这 5 趟参与结果）

| run_id | 条件 | 代数 | 调用 | 状态 | 留出评测 |
|---|---|---:|---:|---|---|
| `E_s42_7f878e7097` | E | 20 | 1,742 | completed | ✅ 5 检查点 |
| `F_s42_7f878e7097` | F | 20 | 1,673 | completed | ✅ 5 检查点 |
| `E_s43_7f878e7097` | E | 20 | 1,770 | completed | ✅ 5 检查点 |
| `F_s43_7f878e7097` | F | 20 | 1,650 | completed | ✅ 5 检查点 |
| `E_s44_7f878e7097` | E | 7 | 501 | **aborted**（operator_cancelled） | 仅 0/5，其余 NA |
| `F_s44_7f878e7097` | F | 0 | 0 | **aborted**（未启动） | 跳过 |

第三对在跑完第 6 代后由操作者取消（当时任何留出分数都尚未产生），如实记录，不参与配对。

## 2 开发期尝试（不进结论，保留为证据）

| run_id | 状态 | 代数 | 中止原因 | 说明 |
|---|---|---:|---|---|
| `E_s42_2da8ef24c9` | completed | 20 | — | 协议 v2；同批 F 在第 4 代中止，整组作废 |
| `E_s42_585625ac42` | aborted | 12 | invalid_generation | 协议 v1；重试提示与上次逐字相同，模型连续交同一题 |
| `E_s42_bb84e5f0ad` | interrupted | 17 | 人工停止 | 协议 v3；单代耗时涨到 22 分钟 |
| `E_s42_f7853f3da0` | aborted | 16 | invalid_generation | 协议 v4；历史签名仍反复撞车 |
| `E_s43_585625ac42` | aborted | 6 | transport_error | 协议 v1 |
| `E_s43_2da8ef24c9` | interrupted | 1 | 人工停止 | 协议 v2 |
| `F_s42_585625ac42` | aborted | 5 | invalid_generation | 协议 v1；S6 输出空串 |
| `F_s42_2da8ef24c9` | aborted | 4 | invalid_generation | 协议 v2；同上 |
| `F_s42_f7853f3da0` | interrupted | 9 | 人工停止 | 协议 v4 |
| `F_s43_585625ac42` | interrupted | 6 | 人工停止 | 协议 v1 |
| `b6_smoke_01` | completed | 1 | — | B6 硬卡口的一代联调 |
| `pilot_01` | completed | 5 | — | pilot，`fitness_window=2` |
| `pilot_win1` | completed | 5 | — | pilot，`fitness_window=1` |

每一代的中止原因与其修复见 `docs/B_协议偏离说明.md` 第 3 节。

## 3 证据文件索引

| 内容 | 路径 |
|---|---|
| 冻结协议（含 6 个注册成员、祖先、数据摘要） | `runs/protocol_v5.json` |
| 校准报告（77.2%，360 次） | `runs/calibration_v5.json` |
| 祖先清单（供 A4 读取） | `runs/ancestors.json` |
| 每趟汇总 | `runs/summaries/<run_id>.json` |
| 留出评测（逐检查点、分题型、逐题） | `runs/evaluations/<run_id>.json` |
| 交错评测计划 | `runs/plans/` |
| 代级快照（题目、参考值、槽位、排名、RNG） | `runs/snapshots/<run_id>/gNN.json` |
| 机读结果简报 | `runs/result_brief.json` |
| 人读交付简报 | `docs/B_交付简报.md` |
| 大屏三张图数据 | `docs/charts/ancestor_curve.csv`、`ef_pair.csv`、`challenge.csv` |
| 调用审计 | `logs/calls.jsonl`、`logs/raw/<run_id>/` |

## 4 复算方式

```bash
python tools/build_result_brief.py                  # 结果简报（不调用模型）
python tools/build_charts.py                        # 三张图数据 + 祖先接口复核
python -m app.run --pair E_s42_7f878e7097,F_s42_7f878e7097
python -m app.run --pair E_s43_7f878e7097,F_s43_7f878e7097
```

`runs/` 与 `logs/` 不进 git（含原始响应与密钥脱敏索引），交接时整目录拷贝。

## 5 断点恢复验证（2026-11-14 15:32）

规格 §15 / B6 验收要求「从 completed 快照续上，gen+1，挑战不重复注入」。用中止的
`E_s44`（末代 6）做了实测，续跑到第 8 代：

```bash
python -m app.run --config config.local.json --resume E_s44_7f878e7097 \
                  --branch demo_recovery --to-gen 8
```

| 检查项 | 结果 |
|---|---|
| 起点 | 已完成到第 6 代；源里另有一条非完成代 g07，只作证据，不作续跑点 |
| 续跑写入 | `runs/snapshots/demo_recovery/`，第 7、8 代 completed |
| 代纪录 | g07：72 槽位 / 83 调用；g08：78 槽位（含挑战 6）/ 88 调用 |
| 历史继承 | 汇总调用 672 = 源 501 + 83 + 88，没有从零开始 |
| 分支标记 | manifest `parent_run_id=E_s44_7f878e7097`、`from_gen=6` |
| 源记录不变 | `E_s44` 仍是 aborted / 7 代，未被覆盖 |
| 挑战不重复 | g08 的 X04 `active` 且 `observations` 恰好 1 条；X05 仍 `scheduled` |
| 评测隔离 | `--evaluate demo_recovery` 被拒绝：`Demo branches are not independent evaluation runs` |

分支状态是 `interrupted`（`--to-gen 8` < n_gen 20），符合预期：演示分支不是新的正式重复。
