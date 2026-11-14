# B 引擎修复与运行说明

本次修复基于 GitHub 的 `cc0a0f2`。保留 A 的执行器和 C 的数据、前端；B 使用
`dataset_adapter.py` 接入已提交的 JSON 题库。`llm.py` 同时提供原有同步 `complete()`
和演化引擎需要的异步 `LLMClient`，避免互相覆盖接口。

## 修复范围

| 审查问题 | 当前行为 |
|---|---|
| 中间检查点读取了下一代 | 所有检查点只读该代 `evaluated_population` |
| g0 读取当前种子文件 | 开跑前保存不可覆盖的 g00 快照及初始种群 |
| 恢复覆盖失败记录、汇总漏掉历史 | 原 run 不再续写；`--resume` 创建带 parent/from_gen 的演示分支，汇总包含继承历史 |
| 算子网络故障退化成克隆后报成功 | 网络故障中止整代；只有预登记的内容格式失败可采用克隆回退 |
| 状态部分更新后失败 | 在状态副本执行代事务，整代成功才提交 RNG、种群、基因历史与挑战状态 |
| 预算只是配置字段 | `max_calls` 限制实际 HTTP 尝试（含重试），`wall_limit_s` 限制等待、请求与退避；触限归档 aborted |
| 异常没有失败快照 | 运行异常/取消记录为 aborted，并保留已知调用及未知用量标志 |
| 恢复只比较签名集合 | 比较完整有序数据、分组、参数、参考值、代码和提示词指纹 |
| 记账漏出题重试与算子 | 记录全部角色调用、HTTP 尝试、call IDs 和逐题原始答案；用量缺失不能记为 0 |
| 留出卷过早可见、配置可替换 | 核对冻结配置、代码、留出卷指纹，并要求组内全部运行 completed/aborted |
| 污染检查只看检查点、仍输出分数 | 扫描所有代及失败代的已暴露签名，重叠直接拒绝评测 |
| E/F 任意配对 | 检查条件、实验种子、数据种子、代数、题集、模型等配置、初始群体、祖先和协议 |
| 正式运行入口缺失 | 新增校准、冻结协议、注册运行、完整代驱动和全组交错评测 |
| 演示漏发实际题面 | 题面与答案格式始终放在独立 user 消息 |
| 演示把请求错误当能力失败 | 保存失败 pass，停止学习和提升结论；同名 pair 拒绝覆盖 |
| Windows 克隆后题库哈希失败 | `.gitattributes` 固定 `data/*.json` 为 LF，保留现有 manifest 的字节校验 |

## 开发与正式命令

先安装 `requirements.txt`。复制 `config.dev.json` 为被 Git 忽略的
`config.local.json`，填写固定模型 ID、OpenAI 兼容 `base_url`、环境变量名。
API key 仅放在对应环境变量中。

预算必须明确配置：`budget_amount: null`，并设置正数 `wall_limit_s` 或正整数
`max_calls`，也可同时设置。当前没有可靠的货币费用上界，因此非空 `budget_amount`
会阻止真实运行。不能把调用数限额当成费用承诺。运行时长和调用上限应依据实际 pilot
选择；正式协议锁定之后不可修改这些值。

```bash
# 只计算理论调用数，不使用 API key、不发模型请求
python -m app.run --config config.local.json --dry-run --all-seeds

# 校准：6 个种子 × 20 题 × 3 次；保存分数、逐题答案与前三个祖先
python -m app.run --config config.local.json --calibrate runs/calibration.json

# 开发期试跑 5 代，使用校准选出的祖先；短 pilot 不注入挑战
python -m app.run --config config.local.json --pilot --gens 5 --calibration runs/calibration.json --run-id pilot_01

# 看完 pilot 的耗时、调用与用量，确定最终配置后再锁定协议
python -m app.run --config config.local.json --lock-protocol runs/protocol.json --calibration runs/calibration.json --seeds 42,43,44

# 启动协议中预登记的全部 E/F 运行；也可用 --run-id 指定一个注册 ID
python -m app.run --protocol runs/protocol.json --all-seeds

# 组内每趟完成或失败之后，先查看冻结的交错评测计划
python -m app.run --protocol runs/protocol.json --evaluate-all --plan-only

# 执行全组评测；run / checkpoint / individual / task 一起打散
python -m app.run --protocol runs/protocol.json --evaluate-all

# 用实际注册 ID 配对
python -m app.run --pair E_RUN_ID,F_RUN_ID

# 演示续跑：源记录不变，分支不计作新的正式种子，也不能做正式评测
python -m app.run --resume ORIGINAL_RUN_ID --branch demo_recovery
```

不带 `--calibration` 的 pilot 用初始前三策略作临时祖先，适合连通性检查。
正式协议必须使用匹配的成功校准报告，平均通过率须在 20%–80%，并冻结 E/F 配对。
锁定前的 pilot 检查由操作者完成；CLI 不会替操作者判断真实耗时和预算是否充足。

时间与 HTTP 限额按一个 `LLMClient` 会话执行：每趟演化独立；全组留出评测共用一个
会话和预算。配置限额时要预留全组评测的调用数，例如 20 代、E/F 各 3 趟的完整
评测有 3,600 个槽位，网络重试还需要额外尝试额度。

## 证据与兼容性

- `runs/manifests/`：冻结配置、初始种群、祖先、数据/源码指纹与分支来源。
- `runs/snapshots/<run_id>/gNN.json`：不可覆盖的 completed/aborted 代快照，g00 为初始群体。
- `runs/summaries/`：包含整趟历史的运行状态、调用数量和预算消耗。
- `runs/plans/`、`runs/evaluations/`：冻结顺序、逐题结果、NA 与严格配对元数据。
- `logs/raw/`、`logs/calls.jsonl`：按会话和 HTTP 尝试保留响应与索引，密钥脱敏。

评测开始标记不可覆盖。调用失败导致受影响检查点为 NA，不算能力答错；异常中断也
不能删除标记后重测选优。旧的简化快照缺乏完整状态和指纹，将被拒绝恢复/正式评测；
旧记录应保留为历史证据，使用新的 run ID 开始新实验。

`app.demo` 的同步调用是独立的双跑演示入口。正式实验的统一限流、硬限额和逐 HTTP
审计属于 `app.run` 的异步链路，不应把双跑演示当成正式 E/F 实验。

## 验证范围

运行 `python -m pytest` 执行 A/B/C 现有测试与新增 B 回归。
`tests/test_b_regressions.py` 覆盖预算并发、请求超时、算子失败事务回滚、冻结 g0、
中间检查点、失败记录保留、恢复指纹、早评测阻断、污染检查、组间交错、错误配对、
演示题面及调用失败处理，并用提交的真实题库与 HTTP 替身联调 CLI。

以上属于离线软件验证。真实模型的校准、pilot、正式 E/F 和留出实验尚未在本次修复中
执行；仓库内 mock 前端数据没有被替换成真实结果。
