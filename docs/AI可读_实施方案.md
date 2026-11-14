# 给 AI 的实施方案（当前版：含实时交互层）

> 用法：整份贴给一个编码 agent，再从 §9 里挑**一段**角色 prompt 贴在同一次对话里。
> 本文所有签名、字段名、返回形状都是**冻结的**。你的任务是按契约实现，不是重新设计。

---

## 1. 你要做什么

在一个已有大量代码的仓库里，追加一个**考研数学助教**子系统。它不重写判分，也不重写演化引擎，只做四件事：

1. 记录学生每次答题（L1 事件）；
2. 用 BKT 估计每个知识点的掌握度；
3. 决定"下一题出什么、给几级提示"；
4. 让前端既能**回放**一段完整对话，也能**实时**接受真人输入。

第 4 条是本版新增的重点：前端不是视频，人可以打字。

---

## 2. 仓库现状（先读，不要猜）

| 路径 | 是什么 | 你动不动 |
|---|---|---|
| `app/executor.py` | 判分内核：`statement / validate_task / normalize_task / solve_exact / solver_view / signature / judge / parse_*` | **不动**，只调用 |
| `app/dataset_adapter.py` | `FileDataset(data_dir="data")`：`.common_dev(gen)` `.challenge_pool()` `.signatures()` | 只读 |
| `data/` | 125 题（calibration 20 / common_dev 80 / challenge_pool 5 / holdout 20） | **不动** |
| `app/serve.py` | 标准库静态服务，已有 `do_POST` 与 `_json()`（服务 `/api/rollback`），启动会 `os.chdir(WEB)` | **只加分支** |
| `app/config.py` | `Config.load(path)`，字段含 `max_calls` | 只读 |
| `app/store.py` | `SnapshotStore(root, run_id)`，`.run_dir` `.load_generation(gen)` | 只读 |
| `app/run.py` | 演化 CLI | 只转调 |
| `web/` | 现有 `index / evolve / result` 三个单文件页 | 只加新页 |
| `tests/` | 439 条用例全绿 | 只加，不改断言 |
| `docs/完整方案_日新考研助手.md` | 公式与规则的最终依据 | 冲突时以它为准 |
| `docs/分工_12小时_详细版.md` | 每人每步与验收 | 你的任务清单 |

先跑一次确认环境：

```bash
python -m pytest -q
python -c "from app.executor import judge, statement; print('ok')"
```

---

## 3. 铁律（违反任一条即返工）

1. **不许改** `app/executor.py`、`app/scoring.py`、`app/ancestor.py`、`data/`、现有测试断言。
2. **不许加第三方依赖**；前端零外链（断网要能跑）。
3. 判分**只能**调 `executor.judge()`；不许自己写比较逻辑，也不许让模型判对错。
4. 题面**只能**来自 `executor.statement()`；`reference` **永远不出现在** `web/*.json`，也不出现在 `/api/ask` 的响应里。
5. 随机一律 `random.Random(seed)`，默认 `seed=42`；同 seed 两次运行结果逐字节一致。
6. 时间一律 ISO 8601 带时区：`2026-11-14T11:53:00+08:00`。
7. 浮点只允许出现在统计量（掌握度、gain）；答案与判分全程有理数/整数。
8. 数据坏了抛 `ValueError`，缺文件抛 `FileNotFoundError`，不要抛裸 `Exception`。
9. 每模块配 `tests/test_<name>.py`，**先写测试再写实现**，测试不联网。
10. 提交信息：`feat(tutor): <做了什么>｜验收：<命令>`。
11. **回放与实时物理隔离**：回放读 `web/tutor.json`（只读）；实时写 `runs/tutor/<sid>/`（可写）。演示 `sid=demo`，实时 `sid=live01`，互不覆盖。
12. `app/serve.py` 会 `os.chdir(WEB)`，所以 Tutor 内部所有路径必须用 `ROOT = Path(__file__).resolve().parents[1]` 拼**绝对路径**。

---

## 4. 冻结契约（逐字实现）

### 4.1 `tutor_profile/1`（`skills/kaoyan-tutor/assets/profile.json`）

```json
{
  "schema": "tutor_profile/1",
  "version": "manual-v1",
  "frozen": true,
  "genes": {"solve_prompt": "", "set_prompt": "", "self_check": true, "teach_prompt": ""},
  "params": {
    "explain_grain": "step",
    "probe_rounds": 2,
    "example_count": 1,
    "hint_ladder": ["restate", "theorem", "step", "example", "answer"],
    "difficulty_offset": 0.1
  },
  "evidence": {"source_run": "manual", "n_gen": 0, "gain": null, "cost": null, "fitness": null},
  "limits": {
    "kc_scope": ["integral_definite", "integral_improper", "det"],
    "answer_domain": "integer | rational | finite decimal",
    "refuse": ["证明题", "英语作文", "政治大题", "任何无唯一答案的题"]
  }
}
```

### 4.2 L1 事件（`runs/tutor/<sid>/events.jsonl`，一行一条）

```json
{"t":"2026-11-14T11:53:00+08:00","sid":"live01","task_id":"T0412",
 "kc":"integral_definite","difficulty":0.62,"correct":false,
 "hint_level":1,"elapsed_s":184,"err":"concept","answer":"1/2","ref_kind":"rational"}
```

固定 11 字段；`err ∈ {sign,calc,concept,misread,giveup,none}`；`answer` 保留原串（截断 100 字）。

### 4.3 `web/tutor.json`（回放数据，只读）

```json
{
  "profile_version": "manual-v1",
  "generated_at": "2026-11-14T15:40:00+08:00",
  "student": {"sid": "demo", "label": "示例学生"},
  "kcs": [
    {"id": "integral_definite", "name": "定积分", "mastery": 0.31, "items": 12, "hint_avg": 1.8},
    {"id": "integral_improper", "name": "反常积分", "mastery": 0.64, "items": 9, "hint_avg": 1.1},
    {"id": "det", "name": "行列式", "mastery": 0.78, "items": 6, "hint_avg": 0.4}
  ],
  "turns": [
    {"n": 1, "role": "tutor", "kc": "integral_definite",
     "statement": "计算定积分 ∫_{0}^{1} (3x² - 2x + 1) dx。",
     "answer_format": "integer, fraction or finite decimal",
     "why": {"mastery": 0.31, "difficulty": 0.35,
             "last_error": "换元后忘记换上下限", "memory": ["evt_0031", "evt_0044"]}},
    {"n": 2, "role": "student", "answer": "1/2", "verdict": "wrong", "err": "calc"},
    {"n": 3, "role": "tutor", "hint_level": 0,
     "text": "题目给的是 0 到 1，你换元之后上下限变成了什么？"},
    {"n": 4, "role": "student", "answer": "7/6", "verdict": "right", "err": "none", "hint_used": 1}
  ],
  "review_plan": [{"kc": "integral_definite", "due": "2026-11-17", "reason": "11/12 独立做对 2 次"}],
  "memory_timeline": [{"t": "2026-11-10", "text": "换元忘换限", "conf": 0.8, "src": ["evt_0031"]}]
}
```

约束：`kcs` 恰 3 条；每条 `role="tutor"` 的轮次必须有 `why`，且三个数字来自真实计算。

**已落地实现（A 组，2026-11-14）**，B/C 按这个读，不要按上面的示意 JSON 猜：

- `why` 实为 **5 个键**：`mastery` / `difficulty` / `gap` / `last_error` / `memory`。
  三个数字是 `mastery`（BKT 折叠值）、`difficulty`（DifficultyBook）、
  `gap = difficulty - mastery`（>0 表示这题略高于当前水平）。
  `last_error` 已是中文短句（由 err kind 翻译），前端直接显示即可。
- `review_plan` 是**下次复习计划**（due 可以在生成时间之后），由
  `schedule.plan_items()` 产出；`schedule.due_items()` 只回答「今天到期哪些」，
  别拿它生成计划，否则数组恒为空。
- `memory_timeline[].conf` = 该错题之后该 KC 的 BKT 掌握度（越高表示
  错因越已被克服），不是随手写的 0.8。
- 生成器 `tools/tutor_cli.py` 离线、幂等（固定时间戳），
  `--turns 4` / `--turns 8` 都通过；输出前自检会拦住敏感字样。

### 4.4 `web/compare.json`

```json
{
  "seed": 42,
  "students": ["weak", "mid", "strong"],
  "policies": ["loose", "verbose", "answer_only"],
  "rows": [
    {"student": "weak", "policy": "loose", "gain": 0.412, "cost": 0.280,
     "fitness": 0.342, "items": 20, "hints": 14, "items_to_mastery": 17}
  ],
  "winner": {"student": "mid", "policy": "loose", "reason": "同增益下成本最低"}
}
```

`rows` 长度必须 = `len(students) × len(policies)`（3×3=9）。`gain/cost/fitness` 保留 3 位小数。

### 4.5 `POST /api/ask`（实时接口，本版新增）

**请求**

```http
POST /api/ask
Content-Type: application/json

{"sid": "live01", "action": "submit", "answer": "7/6"}
```

`action ∈ {start, submit, skip, hint, why, reset}`；`submit` 必带 `answer`（`null` 表示空答）；`start`/`reset`/`why` 忽略 `answer`。

**响应（200）**

```json
{"ok": true, "turn": 3, "kc": "integral_definite",
 "verdict": "wrong", "err": "calc",
 "statement": "计算定积分 ∫_{0}^{1} (3x^2 - 2x + 1) dx。",
 "answer_format": "integer, fraction or finite decimal",
 "hint": {"level": 0, "text": "题目给的是 0 到 1，你换元之后上下限变成了什么？"},
 "mastery": {"integral_definite": 0.28, "integral_improper": 0.64, "det": 0.78},
 "why": {"mastery": 0.31, "difficulty": 0.35,
         "last_error": "换元后忘记换上下限", "memory": ["evt_0031"]},
 "review": [{"kc": "integral_definite", "due": "2026-11-17"}],
 "elapsed_ms": 42}
```

**错误（500 / 400）**

```json
{"ok": false, "error": "Tutor: ..."}
```

规则：
- 任何异常都返回 `{"ok":false,"error":...}`，**不许让进程死**；
- 不加 CORS，只绑 `127.0.0.1`；
- API key 只在服务端（只有 L2 生成式讲解才需要，本版不实现）；
- 状态落 `runs/tutor/<sid>/{events.jsonl,state.json}`，服务重启后再 `start` 必须能接着上一次的掌握度。

---

## 5. 模块与签名

### 5.1 A 组：学生模型

```python
# app/student/kc.py
KC_IDS: tuple[str, ...] = ("integral_definite", "integral_improper", "det")
KC_NAMES: dict[str, str] = {"integral_definite": "定积分",
                            "integral_improper": "反常积分",
                            "det": "行列式"}
def kc_of(task: dict) -> str: ...
def audit(data_dir: str = "data") -> dict[str, int]: ...   # 任一为 0 → ValueError
```

```python
# app/student/bkt.py
@dataclass(frozen=True)
class BKTParams:
    init: float = 0.20
    transit: float = 0.15
    slip: float = 0.10
    guess: float = 0.25

DEFAULT: dict[str, BKTParams]

def update(p: float, correct: bool, params: BKTParams = BKTParams()) -> float: ...
def mastery_of(kc: str, events: list[dict], params: dict | None = None) -> float: ...
def level(mastery: float) -> int: ...      # 0..5
```

```python
# app/student/difficulty.py
class DifficultyBook:
    def __init__(self, path: str | None = None): ...
    def prior(self, task: dict) -> float: ...                  # sha256(signature) → [0.25, 0.75]
    def get(self, task: dict) -> float: ...
    def update(self, task: dict, theta: float, correct: bool) -> float: ...
    def pick(self, candidates: list[dict], theta: float, offset: float = 0.1) -> dict: ...

def expected(theta: float, d: float) -> float: ...
def update_rating(rating: float, other: float, correct: bool, k: float = 16) -> float: ...
```

```python
# app/student/schedule.py
def item_after(correct: bool, interval: float, ease: float) -> tuple[float, float]: ...
def due_items(events: list[dict], now, ease: float = 2.5) -> list[dict]: ...
def plan_items(events: list[dict], ease: float = 2.5) -> list[dict]:
    """所有 KC 的下次复习计划，不过滤是否到期；review_plan 用它。"""
```

```python
# app/student/diagnose.py
ERR_KINDS = ("sign", "calc", "concept", "misread", "giveup", "none")
def classify(task: dict, raw_answer: str | None) -> str: ...
def next_task(pool: list[dict], mastery: dict[str, float], book: DifficultyBook,
              rng, weakest_first: bool = True) -> dict: ...
def why(task: dict, mastery: dict[str, float], book: DifficultyBook,
        last_err: str | None, memory_ids: list[str]) -> dict: ...
```

### 5.2 B 组：状态、记忆、提示、对话、接口

```python
# app/student/events.py
FIELDS = ("t","sid","task_id","kc","difficulty","correct",
          "hint_level","elapsed_s","err","answer","ref_kind")
def append(path, event: dict) -> None: ...   # 缺字段/类型错 → ValueError
def read(path) -> list[dict]: ...            # 不存在 → []
```

```python
# app/student/profile.py
def load(path) -> dict: ...                  # 不存在 → {"schema":"profile/1","facts":[]}
def save(path, prof: dict) -> None: ...
def upsert(prof: dict, key: str, value: str, *, src: list[str],
           t: str, conf: float = 0.8) -> dict: ...   # 同 key 追加 supersedes，不覆盖
```

```python
# app/tutor/memory.py
def score(fact: dict, kc: str, now) -> float: ...          # 0.5·kc + 0.3·exp(-Δt/14) + 0.2·conf
def context(prof: dict, kc: str, now, k: int = 4) -> str: ...   # ≤200 字，空返 ""
```

```python
# app/tutor/hint.py
LADDER = ("restate", "theorem", "step", "example", "answer")
def next_level(current: int, correct: bool) -> int: ...    # 错 +1（封顶 4），对不变
def render(level: int, task: dict, kc: str) -> str: ...    # 纯模板，不调模型
```

```python
# app/tutor/dialog.py
class Tutor:
    def __init__(self, *, sid: str, pool: list[dict],
                 root: str = ".", book: DifficultyBook | None = None, seed: int = 42): ...
    def handle(self, action: str, answer: str | None = None) -> dict: ...  # 六动作统一入口
    def next_question(self) -> dict: ...
    def submit(self, answer: str | None) -> dict: ...
    def report(self) -> dict: ...
    def reset(self) -> None: ...
```

`handle()` 就是 `/api/ask` 的实现入口，直接返回 §4.5 的响应体。

### 5.3 `app/serve.py`（只加分支，不改既有行为）

```python
ALLOW_TUTOR = False

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path == "/api/rollback":
            ...                      # 既有逻辑，不动
        elif path == "/api/ask":
            self._handle_ask()
        else:
            self.send_error(404, "没有这个接口")

    def _handle_ask(self) -> None:
        # 1) 读 Content-Length，json.loads，失败 → 400
        # 2) ALLOW_TUTOR 为 False → 501 {"error":"服务未开启实时（启动时加 --allow-tutor）"}
        # 3) tutor = _get_tutor(sid)（模块级 dict 缓存，key=sid）
        # 4) res = tutor.handle(action, answer)
        # 5) self._json(200, res)；任何异常 → self._json(500, {"ok":False,"error":...})
```

启动参数新增 `--allow-tutor`（默认关闭）。`socketserver.TCPServer` 单线程，**不要加锁**。

### 5.4 C 组：前端（单文件，零外链）

必须存在的元素 id：

| id | 用途 |
|---|---|
| `#mode` | 模式开关：`replay` / `live` |
| `#kcs` | 三条掌握度，`data-kc` 带 KC id |
| `#turns` | 对话流容器 |
| `#why-panel` | 理由面板（默认折叠） |
| `#memory-timeline` | 记忆时间轴 |
| `#review-list` | 复习计划 |
| `#version` | `profile_version` |
| `#answer` | 实时模式的输入框 |
| `#send` | 提交按钮 |

交互要求：

1. 点某条 tutor 消息 → `#why-panel` 展开并填入该轮 `why`；
2. 点 KC 横条 → 高亮并过滤 `#turns`；
3. 点记忆条目 → 展开 `src`；
4. 实时模式：Enter 或 `#send` → `POST /api/ask {action:"submit"}`；另有四按钮「我不会 / 再提示一点 / 为什么给我这道题 / 重置」；
5. **请求失败必须不白屏**：提示"实时模式不可用"，自动切 `replay` 并加载 `web/tutor.json`；
6. 三页导航：`cover.html ↔ tutor.html ↔ compare.html`。

---

## 6. 行为细则（照算，不要自创）

### 6.1 BKT

```
答对：p1 = p(1-slip) / ( p(1-slip) + (1-p)·guess )
      p' = p1 + (1-p1)·transit
答错：p1 = p·slip / ( p·slip + (1-p)(1-guess) )
      p' = p1 + (1-p1)·transit
```

自测（`init=0.20, transit=0.15, slip=0.10, guess=0.25`，已核算）：

| 情形 | 结果 |
|---|---|
| 0.20 → 答对一次 | **0.5526** |
| 0.20 → 答错一次 | **0.1774** |
| 0.20 → 连对两次 | **0.8440** |

写进测试断言（`abs(x-期望) < 1e-3`），不要现场重推。

### 6.2 Elo-lite

```
E = 1 / (1 + 10 ** ((d - theta) / 400))     # 通用 Elo：d、theta 是 1500 量级
```

**本项目的刻度修正（必须照这个实现）**：`prior()` 把难度与能力都归一到 `[0,1]`，
不能把 0.5 直接代进上式——`(d-theta)/400 ≈ 0.001`，`E` 会恒等于 0.5，
难度校准彻底失效。做法是把 `[0,1]` 线性映回 Elo 刻度再套公式：

```
d_elo     = 1000 + 400·d
theta_elo = 1000 + 400·theta
→ (d_elo - theta_elo) / 400 = d - theta
→ E = 1 / (1 + 10 ** (d - theta))
```

`d=0.8, theta=0.5` 时 `E≈0.334`，符合「题比人难，答对概率低」。
`update_rating` 在 Elo 刻度上做 `+k·(实际-E)` 再映回 `[0,1]`，
所以一次答题最大变动 `k/400 = 16/400 = 0.04`，不会一步跳爆。
`DifficultyBook.update` 更新的是**题目难度**，方向相反：学生答对 → 难度下调。

`pick()`：取 `|d - (theta + 0.1)|` 最小的题；同分可传 `rng` 打散，
不传则按 signature 字典序，保证同 seed 可复现。

### 6.3 SM-2 简化

```
答对：interval' = max(1, interval * 1.7 * ease)；ease' = min(2.8, ease + 0.05)
答错：interval' = 1；ease' = max(1.3, ease - 0.2)
```

### 6.4 错因判据（按序命中即返回）

1. `answer` 为 `None` 或判为 `unanswered` → `giveup`
2. 数值等于 `-reference` 且不等于 `reference` → `sign`
3. 与 `reference` 同分母、分子差 1 → `calc`
4. 与 `reference` 相差整数倍（分母整除）→ `calc`
5. `type == "det"` 且差值可被 2 整除 → `misread`
6. 其余 → `concept`

实现必须用 `Fraction`，不许浮点比较。

**判据 4 的准确解读**：`answer / reference` 化简后**分母为 1 且不等于 1 倍**
（如答 `3/2`、参考 `3/4`，差 2 倍）。**不要**写成「差值是整数 → calc」：
det 的差值几乎总是整数，那样会把判据 5 的 `misread` 全部吃掉。
`reference = 0` 时跳过这一条（不能除零），也别把 `-0 == 0` 误判成 `sign`。

**判分边界**：判对错只由 `executor.judge()` 决定。`classify()` 在 judge 判
`unanswered` 时直接返回 `giveup`、判 `right` 时返回 `none`，只有 `wrong`
才取出学生写的数做分类——它不是第二套判分实现。
参考值结构损坏时由 `_ref_fraction` 抛 `ValueError`，上层应中止而不是
记成一次答错。

### 6.5 提示梯度

| 级 | 名称 | 何时给 |
|---|---|---|
| 0 | `restate` | 第一次答错 |
| 1 | `theorem` | 第二次答错 |
| 2 | `step` | 第三次答错 |
| 3 | `example` | 第四次答错 |
| 4 | `answer` | 第五次或学生明确要求 |

`next_level(current, correct=False) → min(4, current+1)`；答对返回 `current`。

### 6.6 fitness

```python
def gain(pre: float, post: float) -> float:
    return 0.0 if pre >= 1 else (post - pre) / (1 - pre)

def cost(items: int, hints: int, base_items: int = 20, base_hints: int = 20) -> float:
    return 0.3 * (items / base_items) + 0.7 * (hints / base_hints)

def score(g: float, c: float, weight: float = 0.25) -> float:
    return g - weight * c
```

### 6.7 模拟学生

```python
class SimStudent:
    def __init__(self, level: str, seed: int = 42): ...   # weak / mid / strong
    # 基础答对率 0.25 / 0.50 / 0.75；用了第 L 级提示再 +0.1·L，上限 0.95
    def answer(self, task: dict, hint_level: int = 0) -> str | None: ...
```

**错误答案必须是合法字面量**（整数 / `a/b` / 有限小数），否则会被判 `unanswered`，成本指标失真。错法只用两种：符号翻转、分子 ±1。

---

## 7. 验收命令（全绿才能提交）

```bash
# 全量回归：不许新增 failure
python -m pytest -q

# A 组
python -m pytest -q tests/test_bkt.py tests/test_difficulty.py tests/test_diagnose.py tests/test_sim.py
python -c "from app.student.kc import audit; a=audit(); assert all(v>0 for v in a.values()), a; print(a)"
python tools/tutor_cli.py --sid demo --turns 8 --out web/tutor.json
python tools/compare.py --seed 42 --out web/compare.json

# B 组
python -m pytest -q tests/test_events.py tests/test_profile.py tests/test_memory.py tests/test_hint.py tests/test_dialog.py
python tools/tutor_demo.py --offline
python skills/prompt-evolver/scripts/export_best.py --run <id> --out /tmp/a.json
python skills/prompt-evolver/scripts/export_best.py --run <id> --out /tmp/b.json
diff /tmp/a.json /tmp/b.json          # 必须为空

# 实时接口（三条 curl 必须全 200）
python app/serve.py --port 8123 --allow-tutor &
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' \
     -d '{"sid":"live01","action":"start"}' | python -m json.tool
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' \
     -d '{"sid":"live01","action":"submit","answer":"0"}' | python -m json.tool
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' \
     -d '{"sid":"live01","action":"reset"}' | python -m json.tool

# 两条硬检查
grep -r "reference" web/*.json && echo "泄漏了，打回" || echo "无参考值，通过"
grep -rE "https?://" web/*.html && echo "有外链，断网会挂" || echo "无外链，通过"
```

**另外必须手验两条**（脚本测不出）：

1. 连交 5 次错答案，`hint.level` 单调升到 4 并封顶；
2. 关掉服务再点页面 → 提示"实时模式不可用"并自动切回放。

---

## 8. 提交与交付

- 小步提交，一条只做一件事；消息模板见铁律 10。
- 不许 `--no-verify`。
- 逐个交付物对照 `docs/分工_12小时_详细版.md` 的「产出」列。
- 来不及就按该文 §7 的降级顺序砍，**不要留一半实现**。

---

## 9. 角色 prompt（挑一段贴在同一次对话里）

### 9.1 A（学生模型 + 对照实验）

```text
你负责 A 组。只改 app/student/、app/tutor/policy.py、app/evalsim/、
tools/tutor_cli.py、tools/compare.py、tests/test_{bkt,difficulty,diagnose,sim}.py。

按顺序做，每步先写测试再写实现，跑通验收再进下一步：
1. app/student/kc.py —— 3 个 KC 与模板映射，audit() 断言都非空
2. app/student/bkt.py —— update/mastery_of/level；自测 0.20→0.5526（对一次）/0.1774（错一次）
3. app/student/difficulty.py —— DifficultyBook + expected + update_rating
4. app/student/schedule.py —— SM-2 简化
5. app/student/diagnose.py —— classify 六条判据 + next_task + why
6. tools/tutor_cli.py —— 8 轮产出 web/tutor.json（严格按契约 4.3）
7. app/tutor/policy.py —— loose / verbose / answer_only 三套 preset
8. app/evalsim/{fitness,sim_student}.py —— 三档学生，错答必须是合法字面量
9. tools/compare.py —— 3×3 产出 web/compare.json（严格按契约 4.4）

禁止：改 executor.py / data/；加依赖；让模型判分；把 reference 写进 web/*.json。
判分一律 executor.judge()。每完成一步，输出「改了什么 + 跑通了哪条命令」。
```

### 9.2 B（记忆 + 提示 + 对话 + /api/ask）

```text
你负责 B 组。只改 app/student/{events,profile}.py、app/tutor/{memory,hint,dialog}.py、
app/serve.py（只加 /api/ask 分支与 --allow-tutor）、skills/、
tools/tutor_demo.py、tests/test_{events,profile,memory,hint,dialog}.py。

按顺序做：
1. skills/prompt-evolver/ 与 skills/kaoyan-tutor/ 两套目录 + SKILL.md（照 docs/skill包装_养蛊机与助教.md 抄）
2. app/student/events.py —— 11 字段 append-only，缺字段抛 ValueError
3. app/student/profile.py —— upsert 追加 supersedes，不覆盖
4. app/tutor/memory.py —— 0.5/0.3/0.2 打分，top-4，≤200 字
5. app/tutor/hint.py —— 五级梯度，纯模板渲染
6. app/tutor/dialog.py —— class Tutor，handle() 六动作，状态落 runs/tutor/<sid>/
7. app/serve.py —— do_POST 加 /api/ask，--allow-tutor 开关，异常转 500 JSON
8. tools/tutor_demo.py --offline + skills/kaoyan-tutor/scripts/{ask,report}.py
9. skills/prompt-evolver/scripts/{evolve,export_best}.py —— 导出幂等

禁止：改 executor.py；让模型判对错；把 hint 文案写进 dialog（必须走 hint.render）；
在 serve.py 里用相对路径（会 chdir 到 web/，一律用 ROOT 绝对拼接）。
每完成一步，输出「改了什么 + 跑通了哪条命令」。
```

### 9.3 C（三页 + 实时输入栏）

```text
你负责 C 组。只改 web/ 与 docs/demo/，不碰 app/。

1. web/tutor.html —— 六块区域（对话流 / 3 条掌握度 / 理由面板 / 记忆时间轴 /
   复习计划 / 版本号），先用契约 4.3 的示例 JSON 内联当假数据。
2. 交互三件：点 tutor 消息展开理由；点 KC 高亮并过滤对话；点记忆展开 src。
3. 顶部模式开关 #mode：replay（默认，读 web/tutor.json）/ live（调 /api/ask）。
4. live 模式下加输入框 #answer + #send，以及四按钮：我不会 / 再提示一点 /
   为什么给我这道题 / 重置。Enter 提交。
5. 请求失败不白屏：提示"实时模式不可用"，自动切 replay 并加载 tutor.json。
6. web/compare.html —— 3×3 对照，条形图可切 gain / cost / fitness。
7. web/cover.html —— 标题 + 一句话 + 三个数字；三页导航互通。

硬约束：单文件、内联 CSS/JS、零外链（断网可用）；不出现 reference 字样；
1280×720 投影下不溢出；对=绿、错=红（学习场景，与股市配色无关）。
每完成一页，输出「页面路径 + 手点了哪三处」。
```

### 9.4 D（可选：如果第四个人或想并行，单独做实时层）

```text
你只做实时层，改 app/serve.py 与 web/tutor.html 的 live 部分，其他文件不动。
后端：/api/ask 按契约 4.5 收发；ALLOW_TUTOR 开关；异常一律 500 JSON；
      路径用 ROOT 绝对拼接；单线程无需锁。
前端：#mode 开关 + #answer/#send + 四按钮；失败自动切回放。
禁止：把 API key 写进前端；开 CORS；让实时成为演示的唯一路径。
验收：curl 三条全 200；连答 5 题；关服务后页面提示并切回放。
```
