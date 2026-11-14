# 给 B：B8 跑批前必须知道的四个坑（已实测，非推测）

> 来源：C 在 `c-final-20261114`（`6b5a15f`）上把跑批入口切到 `app/run.py` 时，
> 用真模型（deepseek-flash）跑了 1 代验证。下面每一条都有落盘证据，可直接复核。
>
> **E/F 六趟仍由 B8 值守执行**（分工方案 v7.3 §515），C 不插手跑批。
> 本文只交付「引擎实测问题 + 修法」，不交付实验数据。

---

## 坑 1（致命）：setter 的 max_tokens=400 会被推理内容吃光，整代作废

**现象**：`python -m app.run --run --pilot --to-gen 1` 第一代直接 `invalid_generation`，
26 次调用全部作废，M=2（6 人只凑出 2 道合规题）。

**证据**（`logs/raw/SMOKE_E1/`，每文件是一次真实 HTTP）：

| slot | attempts | output_tokens | reasoning_tokens | content |
|---|---|---|---|---|
| `g01_set_G00_S2_a1..a6` | 6 次 | 400 / 400 / 400 / 400 / 400 / 400 | **400 × 6** | **全部为空** |
| `g01_set_G00_S1_a1` | 1 次（成功） | 349 | 317 | 勉强挤出 32 token |

**根因**：deepseek-flash 是推理模型，`max_tokens` 先被 `reasoning_content` 吃掉。
400 的预算下，reasoning 占满 400，`content` 一个字都不剩；HTTP 仍返回 200，
所以 `LLMClient` 记为 `status="ok"`，只在 `parse_error` 上失败 —— **看起来像模型不会出题，
实际是我们的额度不够**。

**修法**（C 已改在 `config.json` / `configs/{E,F}.json`）：

```json
"max_tokens_setter": 4000,     // 原 400
"max_tokens_operator": 2000    // 原 500
```

`max_tokens_solver` 保持 12000（C 侧 6000→12000 也是同一个坑）。

**改完实测**：1 代 `status=completed`，M=6，槽位 72，84 次调用，52 秒。

**建议 B 再补一层**：`llm_async` 把「`finish_reason=length` 或 `content` 为空」
标成 `infra`，与「答错」分开。现在它和正常回答一样记 `ok`，
进了统计就会把额度不足算成能力不足 —— 这正是 E/F 对照最不能容忍的系统性偏差。

---

## 坑 2：history 是一份共享文件，直接喂给导出会串趟

**现象**：`SnapshotStore.history_path` = `runs/history.jsonl`，**所有趟写同一份**，
每行靠 `run_id` 区分。而 `app/export.py:load_history()` 与
`app/evaluate_holdout.py:pick_lineages()` 都是按「一趟一个文件」读，
且只按 `gen` 去重、**完全不看 `run_id`**。

**实测**：跑完 3 趟后根文件里 6 行来自 3 个 `run_id`（`SMOKE_B1/E1/E2`），
gen 序列是 `[0,1,0,1,0,1]`。直接导出 → 同代记录互相覆盖，
得到一条「六趟拼接而成、并不存在」的曲线。页面上一切正常，数字全错。

**修法**（C 已交付 `tools/split_history.py`）：

```bash
python tools/split_history.py                 # 全部趟
python tools/split_history.py --run-id EVO_E1 # 单趟
python tools/split_history.py --check         # 只报告不写
```

它按 `run_id` 拆到 `runs/snapshots/<run_id>/history.jsonl`，
每趟一份、按 gen 升序，与 export / 留出评测的读取口径对齐。

---

## 坑 3：manifest 在别处且没有 backend，真数据会被标成假

**现象**：B 写 `runs/manifests/<run_id>.json`（键：`ancestors / config / dataset /
config_fingerprint / run_id / source …`），**没有 `backend` 字段**。
而 C 的「真假自证」只认 `runs/<run_id>/manifest.json` 的 `backend == "real"`，
否则页面挂 MOCK 红标、数字不进结论。

**实测**：不处理时 `export.py` 输出 `is_mock: true`，
理由「没有 manifest.json 能证明是 --backend real 的产出」。

**修法**：`tools/split_history.py` 兼做 manifest 落地，把 `runs/manifests/<id>.json`
转存到 `runs/snapshots/<id>/manifest.json` 并补 `backend` 字段。判定不猜：

- `config.model` / `base_url` 含 `REPLACE_WITH` 或为空 → `backend: "unknown"`
- `logs/raw/<run_id>/` 下没有任何 `status == "ok"` 的调用 → `backend: "unknown"`
- 两者都满足 → `backend: "real"`，并写 `backend_reason` 说明依据

实测结果：`backend=real`，理由「config 非占位符；logs/raw 有 84 条 ok 调用」。
导出随即变成 `is_mock: false`、`reference 0`、`holdout 0`、白名单自检通过。

**建议 B 考虑**：在 `runtime.new_run()` 写 manifest 时直接落 `backend`，
比在下游补更干净 —— 现在是「事后盖章」，源头声明才是正解。

---

## 坑 4：第 0 代语义不同（B 是种子快照，不是跑完的一代）

**现象**：B 的 `g00` 是 `status=completed` 但**没有 `ranking` / `M` / `individuals`**，
只有 `evaluated_population`（6 个种子）。C 的 `g0` 是跑完的一代、有完整排名。

**实测**：`pick_lineages()` 取 `first["ranking"][:2]` 直接 `KeyError: 'ranking'`。

**修法**：没有 `ranking` 时按 `evaluated_population` 的种子顺序取前两名，
并在 docstring 写明两套语义都成立（祖先的意义是「起点」，不是「按分排名」）。

**需要 B 确认**：`configcheckpoints = [0,5,10,15,20]` 里的 checkpoint 0
指的是这个未评测的种子快照吗？如果是，留出评测的第 0 代基线就取的是种子本身，
这一点建议在 protocol 里写明，免得评委问「你的 g0 到底测没测过」。

---

## 另外两件事（不阻塞，但影响「除条件外零差异」的举证）

**① `semantic_fingerprint` 含 condition，证明不了 E/F 同源**

B 的 `Config.semantic_fingerprint()` 是 `to_dict()` 全字段的 sha256，
E 和 F 的指纹必然不同。C 原来的方法是「同一份 config，只差一个 `--freeze` 开关」，
所以 sha 相同本身就是机器可验证的证据。

现在改成：`configs/E.json` 与 `configs/F.json` **只差 `condition` 一个字段**，
脚本末尾自检「去掉 condition 后逐字节相同」。实测通过，
fingerprint(no condition) = `3e39dc820e58147c`。

**② `require_runtime_ready()` 要求成本上限**

`config.json` 原来是 `wall_limit_s: null` 且无 `max_calls`，第一次跑直接被拒：
「必须设置 wall_limit_s 或 max_calls；当前费用未知，不能承诺货币上限」。
C 已给两份配置补 `max_calls = 2800`（E 理论 2289、F 2232，留约 20% 给网络重试）。

**B 侧要重新核这个数**：它同时是 `http_attempts` 的上限（含重试），
真跑六趟前建议按实际网络情况调，不要照抄。

---

## C 已改的文件（都在 `c-final-20261114`）

| 文件 | 改动 |
|---|---|
| `tools/run_official.sh` | 跑批入口切到 `app/run.py`；续跑改用 B 的 `--resume`（派生分支语义）；每趟后自动拆 history + 留出评测 |
| `tools/split_history.py` | 新增。按 run_id 拆 history + 落 manifest（带 backend 判定） |
| `configs/E.json` `configs/F.json` | 新增。只差 `condition`，含 `max_calls` |
| `config.json` | `max_tokens_setter` 400→4000、`max_tokens_operator` 500→2000 |
| `app/evaluate_holdout.py` | 兼容 g0 无 ranking；三处 `relative_to(ROOT)` 收敛成 `_rel()`（路径出仓库就崩） |

`tools/run_official.sh` 用 `--pilot` 绕开了 protocol 流程，**只适合冒烟验证**。
正式六趟建议仍走 B 的 `--calibrate → --lock-protocol → --protocol --all-seeds`，
否则 `protocol.json` 里没有这六趟的注册记录，`--evaluate-all` 的交错评测也用不上。

---

## 一句话给 B8

跑之前先改 `max_tokens_setter`（400 会让你的第一代直接 `invalid_generation`，
26 次调用白烧），跑完先 `python tools/split_history.py` 再导出，
否则六趟的数据会互相覆盖。
