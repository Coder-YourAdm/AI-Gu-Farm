# 给 AI 的实施方案（可直接粘贴执行）

> 用法：把本文整份贴给一个编码 agent，然后从 §9 里挑对应角色的一段贴在同一次对话里。
> 本文所有签名与字段都是**冻结的**，AI 不需要"设计"，只需要按契约实现。

---

## 1. 你是谁、要做什么

你在为一个已经有大量代码的仓库追加一个**考研数学助教**子系统。它不重写判分，也不重写演化引擎，只做四件事：

1. 记录学生每一次答题（L1 事件）；
2. 用 BKT 估计每个知识点的掌握度；
3. 决定"下一道题出什么、给几级提示"；
4. 产出一份前端能直接渲染的静态 JSON。

前端的交互（点开"为什么给我这道题"）全部在浏览器里完成，**不调后端接口**。

---

## 2. 仓库现状（先读这些，不要猜）

| 路径 | 里面是什么 | 你要不要动 |
|---|---|---|
| `app/executor.py` | 六函数判分内核：`statement / validate_task / normalize_task / solve_exact / solver_view / signature / judge` | **不动**，只调用 |
| `app/dataset_adapter.py` | `FileDataset`：`.common_dev(gen)` `.challenge_pool()` `.signatures()` | 只读 |
| `data/` | 125 题：calibration 20 / common_dev 80 / challenge_pool 5 / holdout 20 | **不动** |
| `app/config.py` | `Config.load(path)`，字段含 `max_calls` | 只读 |
| `app/store.py` | `SnapshotStore(root, run_id)`，`.run_dir` `.load_generation(gen)` | 只读 |
| `app/run.py` | 演化 CLI（`--run --pilot --gens`） | 只转调，不改 |
| `web/` | 现有三个单文件页面（`index/evolve/result`） | 只加新页，不改旧的 |
| `tests/` | 439 条用例，全绿 | 只加，不改现有断言 |
| `docs/完整方案_日新考研助手.md` | 方案原文（公式、提示梯度、错因、评测） | 冲突时以它为准 |
| `docs/分工_12小时_详细版.md` | 每人每步的任务与验收 | 你的任务清单 |

**先跑一次**确认环境：

```bash
python -m pytest -q
python -c "from app.executor import judge, statement; print('ok')"
```

---

## 3. 铁律（违反任一条即返工）

1. **不许改** `app/executor.py`、`app/scoring.py`、`app/ancestor.py`、`data/`、现有 `tests/` 的断言。
2. **不许加第三方依赖**（后端只用标准库；已有 `httpx` 可用）。前端零外链（断网要能跑）。
3. 判分**只能**调 `executor.judge()`，**不许**自己写比较逻辑，也不许让模型判对错。
4. 发给"学生"的题面**只能**来自 `executor.statement()`；`reference` 字段**永远不出现在**任何 `web/*.json` 里。
5. 所有随机必须走 `random.Random(seed)`，**默认 seed=42**；同 seed 两次运行结果必须逐字节一致。
6. 时间一律用 ISO 8601 带时区字符串（`2026-11-14T11:46:00+08:00`）。
7. 浮点只允许出现在统计量（掌握度、gain）上；**答案与判分全程有理数/整数**。
8. 函数不抛"裸 Exception"；数据坏了抛 `ValueError`，缺文件抛 `FileNotFoundError`。
9. 每个模块配 `tests/test_<name>.py`，**先写测试再写实现**；测试里不出现网络调用。
10. 提交信息格式：`feat(tutor): <做了什么>｜验收：<命令>`。

---

## 4. 冻结契约（逐字实现，不要改字段名）

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
{"t":"2026-11-14T11:46:00+08:00","sid":"demo","task_id":"T0412",
 "kc":"integral_definite","difficulty":0.62,"correct":false,
 "hint_level":1,"elapsed_s":184,"err":"concept","answer":"1/2","ref_kind":"rational"}
```

字段固定 11 个；`err ∈ {sign,calc,concept,misread,giveup,none}`；`answer` 保留原始字符串（截断到 100 字）。

### 4.3 `web/tutor.json`（前端唯一数据源）

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
    {"n": 3, "role": "tutor", "hint_level": 1,
     "text": "题目给的是 0 到 1，你换元之后上下限变成了什么？"},
    {"n": 4, "role": "student", "answer": "7/6", "verdict": "right", "err": "none", "hint_used": 1}
  ],
  "review_plan": [{"kc": "integral_definite", "due": "2026-11-17", "reason": "11/12 独立做对 2 次"}],
  "memory_timeline": [{"t": "2026-11-10", "text": "换元忘换限", "conf": 0.8, "src": ["evt_0031"]}]
}
```

约束：`turns` 里每条 `role=tutor` 的都必须有 `why`，且 `why` 的三个数字必须来自真实计算；`kcs` 恰 3 条。

### 4.4 `web/compare.json`

```json
{
  "seed": 42,
  "students": ["weak", "mid", "strong"],
  "policies": ["loose", "verbose", "answer_only"],
  "rows": [
    {"student": "weak", "policy": "loose", "gain": 0.41, "cost": 0.28,
     "fitness": 0.34, "items": 20, "hints": 14, "items_to_mastery": 17}
  ],
  "winner": {"student": "mid", "policy": "loose", "reason": "同增益下成本最低"}
}
```

`rows` 长度必须等于 `len(students) × len(policies)`（3×3=9）。`gain` 保留 3 位小数。

---

### 4.5 实时接口（可选层，`POST /api/ask`）

前端默认是**回放模式**（只读 `web/tutor.json`）。如果要做"人能打字"的交互，加这一个接口即可；**不需要模型**，判分与提示都是本地引擎。

```
POST /api/ask      Content-Type: application/json
{"sid":"demo","action":"start","answer":null}
```

`action ∈ {start, submit, skip, hint, why, reset}`；`submit` 必带 `answer`（可为 `null` 表示空答）。

```json
{"ok": true, "turn": 3, "kc": "integral_definite",
 "verdict": "wrong", "err": "calc",
 "statement": "计算定积分 ∫_{0}^{1} (3x^2 - 2x + 1) dx。",
 "answer_format": "integer, fraction or finite decimal",
 "hint": {"level": 1, "text": "…"},
 "mastery": {"integral_definite": 0.28, "integral_improper": 0.64, "det": 0.78},
 "why": {"mastery": 0.31, "difficulty": 0.35, "last_error": "换元后忘记换上下限",
         "memory": ["evt_0031"]},
 "review": [{"kc": "integral_definite", "due": "2026-11-17"}]}
```

状态落盘：`runs/tutor/<sid>/events.jsonl` + `state.json`，重启可续。
**必须绑 `127.0.0.1`**，不加 CORS，密钥只在服务端。
**坑**：`app/serve.py` 会 `os.chdir(WEB)`，Tutor 内部所有路径必须用 `ROOT`（`Path(__file__).resolve().parents[1]`）拼绝对路径。

---

## 5. 模块与签名（实现即照抄，不要改名字）

### 5.1 A 组：学生模型

```python
# app/student/kc.py
KC_IDS: tuple[str, ...] = ("integral_definite", "integral_improper", "det")
KC_NAMES: dict[str, str] = {"integral_definite": "定积分",
                            "integral_improper": "反常积分",
                            "det": "行列式"}
def kc_of(task: dict) -> str: ...
def audit(data_dir: str = "data") -> dict[str, int]: ...   # {kc: 题数}，任一为 0 抛 ValueError
```

```python
# app/student/bkt.py
from dataclasses import dataclass

@dataclass(frozen=True)
class BKTParams:
    init: float = 0.20
    transit: float = 0.15
    slip: float = 0.10
    guess: float = 0.25

DEFAULT: dict[str, BKTParams]

def update(p: float, correct: bool, params: BKTParams = BKTParams()) -> float: ...
def mastery_of(kc: str, events: list[dict], params: dict | None = None) -> float: ...
def level(mastery: float) -> int: ...   # 0..5：<0.2,<0.4,<0.6,<0.75,<0.9,>=0.9
```

```python
# app/student/difficulty.py
class DifficultyBook:
    def __init__(self, path: str | None = None): ...
    def prior(self, task: dict) -> float: ...                 # 由 signature 的 sha256 稳定映射到 [0.25,0.75]
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

### 5.2 B 组：记忆、提示、对话

```python
# app/student/events.py
FIELDS = ("t","sid","task_id","kc","difficulty","correct",
          "hint_level","elapsed_s","err","answer","ref_kind")
def append(path, event: dict) -> None: ...     # 缺字段/类型错 → ValueError
def read(path) -> list[dict]: ...              # 文件不存在 → 返回 []
```

```python
# app/student/profile.py
def load(path) -> dict: ...                    # 不存在 → {"schema":"profile/1","facts":[]}
def save(path, prof: dict) -> None: ...
def upsert(prof: dict, key: str, value: str, *, src: list[str],
           t: str, conf: float = 0.8) -> dict: ...   # 同 key 追加 supersedes，不覆盖
```

```python
# app/tutor/memory.py
def score(fact: dict, kc: str, now) -> float: ...   # 0.5·kc + 0.3·exp(-Δt/14) + 0.2·conf
def context(prof: dict, kc: str, now, k: int = 4) -> str: ...  # ≤200 字，空则 ""
```

```python
# app/tutor/hint.py
LADDER = ("restate", "theorem", "step", "example", "answer")

def next_level(current: int, correct: bool) -> int: ...  # 错+1（封顶 4），对保持不变
def render(level: int, task: dict, kc: str) -> str: ...  # 纯模板，不调模型
```

```python
# app/tutor/dialog.py
class Tutor:
    def __init__(self, *, sid: str, pool: list[dict], profile_path, events_path,
                 book: DifficultyBook | None = None, seed: int = 42): ...
    def next_question(self) -> dict: ...   # {"task","statement","answer_format","why"}
    def submit(self, answer: str | None) -> dict: ...
    def report(self) -> dict: ...          # {"kcs":[...], "review":[{"kc","due","reason"}]}
```

`submit()` 返回：

```json
{"verdict":"wrong","err":"calc","correct":false,
 "mastery":{"integral_definite":0.28},
 "hint":{"level":1,"text":"…"},"memory":["evt_0031"]}
```

### 5.3 C 组：前端（纯 HTML/CSS/JS，单文件）

默认消费 §4.3 与 §4.4 的 JSON（回放模式）；若启用 §4.5，则追加一个"实时"开关，两模式共存。必须存在的元素 id：

| id | 用途 |
|---|---|
| `#kcs` | 三条掌握度横条，`data-kc` 属性带 KC id |
| `#turns` | 对话流容器 |
| `#why-panel` | 理由面板（默认折叠） |
| `#memory-timeline` | 记忆时间轴 |
| `#review-list` | 复习计划 |
| `#version` | `profile_version` |

交互要求：
1. 点击某条 tutor 消息 → `#why-panel` 展开并填入该轮 `why`；
2. 点击 KC 横条 → 高亮该 KC，并过滤 `#turns` 只留该 KC 的轮次；
3. 点击记忆条目 → 展开 `src` 列表；
4. 切页导航：`cover.html ↔ tutor.html ↔ compare.html`。

---

## 6. 行为细则（照这个算，不要自创）

### 6.1 BKT 更新

```
答对：p1 = p·(1-slip) / ( p·(1-slip) + (1-p)·guess )
      p' = p1 + (1-p1)·transit
答错：p1 = p·slip / ( p·slip + (1-p)·(1-guess) )
      p' = p1 + (1-p1)·transit
```

自测（`init=0.20, transit=0.15, slip=0.10, guess=0.25`，已核算）：

| 情形 | 结果 |
|---|---|
| 0.20 → 答对一次 | **0.5526** |
| 0.20 → 答错一次 | **0.1774** |
| 0.20 → 连答对两次 | **0.8440** |

这三个数写进测试断言（`abs(x - 期望) < 1e-3`），不要现场重推。

### 6.2 Elo-lite

```
E = 1 / (1 + 10 ** ((d - theta) / 400))
新 d 或 theta = 旧值 + 16 · ((1 if correct else 0) - E)
```

`pick()`：在候选里取 `|d - (theta + 0.1)|` 最小者；同分用 `rng` 打散。

### 6.3 SM-2 简化

```
答对：interval' = max(1, interval * 1.7 * ease) ；ease' = min(2.8, ease + 0.05)
答错：interval' = 1 ；ease' = max(1.3, ease - 0.2)
```

### 6.4 错因判据（按顺序命中即返回）

1. `raw_answer is None` 或判为 `unanswered` → `giveup`
2. 数值等于 `-reference` 且不等于 `reference` → `sign`
3. 与 `reference` 同分母、分子差 1 → `calc`
4. 与 `reference` 相差整数倍（分母整除）→ `calc`
5. `type == "det"` 且差值可被 2 整除 → `misread`
6. 其余 → `concept`

> 判据实现要能处理 `Fraction`；不要用浮点比较。

### 6.5 提示梯度

| 级 | 名称 | 何时给 |
|---|---|---|
| 0 | `restate` | 第一次答错 |
| 1 | `theorem` | 第二次答错 |
| 2 | `step` | 第三次答错 |
| 3 | `example` | 第四次答错 |
| 4 | `answer` | 第五次或学生明确要求 |

`next_level(current, correct=False)` 返回 `min(4, current+1)`；答对返回 `current`。

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
    def __init__(self, level: str, seed: int = 42): ...   # level ∈ {"weak","mid","strong"}
    # 答对概率：weak 0.25 / mid 0.50 / strong 0.75；用了第 L 级提示再 +0.1·L（上限 0.95）
    def answer(self, task: dict, hint_level: int = 0) -> str | None: ...
```

`answer()` 的**错误答案必须是合法字面量**（整数 / `a/b` / 有限小数），否则会被 `judge` 判成 `unanswered`，成本指标会失真。错法只用两种：符号翻转、分子 ±1。

---

## 7. 验收命令（必须全绿才能提交）

```bash
# 全量回归（不许有新增 failure）
python -m pytest -q

# A 组
python -m pytest -q tests/test_bkt.py tests/test_difficulty.py tests/test_diagnose.py
python -c "from app.student.kc import audit; a=audit(); assert all(v>0 for v in a.values()), a; print(a)"
python tools/tutor_cli.py --sid demo --turns 8 --out web/tutor.json
python tools/compare.py --seed 42 --out web/compare.json

# B 组
python -m pytest -q tests/test_events.py tests/test_profile.py tests/test_memory.py tests/test_hint.py
python tools/tutor_demo.py --offline
python skills/prompt-evolver/scripts/export_best.py --run <id> --out /tmp/a.json
python skills/prompt-evolver/scripts/export_best.py --run <id> --out /tmp/b.json
diff /tmp/a.json /tmp/b.json

# C 组
python app/serve.py --port 8123
# 手点：封面 → 学生端 → 点开理由 → 切 KC → 展开记忆 → 对照页
```

**额外硬检查**（写成测试或 shell 检查）：

```bash
grep -r "reference" web/*.json && echo "泄漏了，打回" || echo "无参考值，通过"
grep -rE "https?://" web/*.html && echo "有外链，断网会挂" || echo "无外链，通过"
```

---

## 8. 提交与交付

- 小步提交，一条提交只做一件事；消息模板：`feat(tutor): 实现 bkt.update｜验收：pytest -q tests/test_bkt.py`
- 交付前更新对应 `tests/`；**不允许**用 `--no-verify` 跳过钩子。
- 交付物清单见 `docs/分工_12小时_详细版.md` 每人「产出」列。
- 若某功能来不及：按 `docs/分工_12小时_详细版.md` §5 的降级顺序砍，**不要留下一半**。

---

## 9. 三份角色 prompt（挑一段贴在同一次对话里）

### 9.1 A（学生模型 + 对照实验）

```text
你负责 A 组。只改 app/student/、tools/tutor_cli.py、tools/compare.py、
app/evalsim/、tests/test_{bkt,difficulty,diagnose,sim}.py。

按顺序做，每步先写测试再写实现，每步跑完验收再进下一步：
1. app/student/kc.py —— 3 个 KC 与模板映射，audit() 断言非空
2. app/student/bkt.py —— update/mastery_of/level，自测 0.20→0.5526（答对一次）/0.1774（答错一次）
3. app/student/difficulty.py —— DifficultyBook + expected + update_rating
4. app/student/schedule.py —— SM-2 简化
5. app/student/diagnose.py —— classify 六条判据 + next_task + why
6. tools/tutor_cli.py —— 跑 8 轮产出 web/tutor.json（严格按契约 4.3）
7. app/tutor/policy.py —— loose / verbose / answer_only 三套 preset
8. app/evalsim/fitness.py + sim_student.py —— 3 档学生，错误答案必须合法
9. tools/compare.py —— 3×3 产出 web/compare.json（严格按契约 4.4）

禁止：改 executor.py / scoring.py / data/；引入新依赖；让模型参与判分；
把 reference 写进任何 web/*.json。判分一律调 executor.judge()。
每完成一步，输出「改了什么 + 跑通了哪条命令」。
```

### 9.2 B（记忆 + 提示 + 对话 + skill 脚本）

```text
你负责 B 组。只改 app/student/{events,profile}.py、app/tutor/{memory,hint,dialog}.py、
skills/、tools/tutor_demo.py、tests/test_{events,profile,memory,hint}.py。

按顺序做：
1. skills/prompt-evolver/ 与 skills/kaoyan-tutor/ 两套目录 + SKILL.md
   （内容照 docs/skill包装_养蛊机与助教.md §3/§4 抄，不要自创）
2. app/student/events.py —— 11 字段 append-only，缺字段抛 ValueError
3. app/student/profile.py —— upsert 追加 supersedes，不覆盖
4. app/tutor/memory.py —— 打分 0.5/0.3/0.2，top-4，≤200 字
5. app/tutor/hint.py —— 五级梯度，纯模板渲染
6. app/tutor/dialog.py —— class Tutor 串起选题/判分/归因/更新/记忆
7. tools/tutor_demo.py --offline —— 不联网跑 8 轮
8. skills/kaoyan-tutor/scripts/{ask,report}.py
9. skills/prompt-evolver/scripts/{evolve,export_best}.py —— 导出幂等

禁止：改 executor.py；让模型判对错；把 hint 文案写进 dialog 里（必须走 hint.render）。
判分一律 executor.judge()；随机一律 random.Random(seed=42)。
每完成一步，输出「改了什么 + 跑通了哪条命令」。
```

### 9.3 C（三个页面）

```text
你负责 C 组。只改 web/ 与 docs/demo/，不碰 app/。

1. web/tutor.html —— 六块区域：对话流 / 3 条掌握度 / 理由面板 / 记忆时间轴 /
   复习计划 / 版本号。先用契约 4.3 的示例 JSON 内联当假数据。
2. 交互三件：点 tutor 消息展开理由面板；点 KC 高亮并过滤对话；点记忆条目展开 src。
3. web/compare.html —— 3×3 对照，条形图可切 gain / cost / fitness。
4. web/cover.html —— 标题 + 一句话 + 三个数字。
5. 三页导航互通；样式沿用现有 web/index.html 的字体与配色。

硬约束：单文件、内联 CSS/JS、零外链（断网可用）；不出现 reference 字样；
投影 1280×720 下不溢出；对=绿、错=红（这是学习场景，与股市配色无关）。
每完成一页，输出「页面路径 + 手点了哪三处」。
```

### 9.4 D（实时交互层，可选，2 小时）

```text
你负责给 tutor.html 加"人能打字"的实时模式。分两半：

后端（只改 app/serve.py，加分支，不动已有 /api/rollback）：
1. do_POST 里加 /api/ask，按契约 4.5 收发 JSON。
2. 用 class Tutor 处理 action：start / submit / skip / hint / why / reset。
3. 状态落 runs/tutor/<sid>/，重启可续；启动开关 --allow-tutor，默认关闭。
4. 所有路径用 ROOT 绝对拼接（serve.py 会 chdir 到 web/）。
5. 任何异常都返回 {"ok":false,"error":"..."} + 500，不要让进程死。

前端（只改 web/tutor.html）：
6. 顶部加模式开关：回放（默认，读 web/tutor.json）/ 实时（调 /api/ask）。
7. 输入框 + Enter 提交；四个按钮：我不会 / 再提示一点 / 为什么给我这道题 / 重置。
8. 请求失败时不白屏：提示"实时模式不可用"，自动切回回放并 load tutor.json。

禁止：把 API key 写进前端；开 CORS；让实时模式成为演示的唯一路径。
验收：断网 + 只看回放，三个页面照常；联网 + 切实时，能连续答 5 题且判分正确。
```
