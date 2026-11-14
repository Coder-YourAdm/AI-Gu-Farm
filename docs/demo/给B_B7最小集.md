# B7 最小可交付清单（给 B / 毛尚胜）

> 目标：让 `web/tutor.html` 的"实时"从本地假 Tutor 切到真后端。
> 前端已经按契约 4.5 做好，**只等一个 `POST /api/ask`**；不缺设计，只缺实现。
> 最小集约 40 分钟；先跑通"连答 5 题"，其余可以后补。

## 一、最小集只做三件

| # | 文件 | 做到什么程度就够 |
|---|---|---|
| 1 | `app/tutor/hint.py` | `LADDER`、`next_level(current, correct)`、`render(level, task, kc)`，纯模板，五级 |
| 2 | `app/tutor/dialog.py` | `class Tutor` + `handle(action, answer)` 六动作；状态放内存即可（落盘后补） |
| 3 | `app/serve.py` | 加 `/api/ask` 分支 + `--allow-tutor`；异常一律转 500 JSON |

`events.py` / `profile.py` / `memory.py` **今天可以先不做**：`why.memory` 先返回 `[]`，
`/api/ask` 只依赖 `Tutor.handle()` 的返回值。等主链路跑通再补。

## 二、`Tutor.handle()` 的返回形状（照抄，字段一个都不能少）

```python
{
  "ok": True,
  "turn": 3,                    # int
  "kc": "integral_definite",
  "verdict": "wrong",           # "right" / "wrong" / None
  "err": "calc",                # sign|calc|concept|misread|giveup|none，正确时为 "none"
  "statement": "……题面……",
  "answer_format": "integer, fraction or finite decimal",
  "hint": {"level": 1, "text": "……"},
  "mastery": {"integral_definite": 0.28, "integral_improper": 0.64, "det": 0.78},
  "why": {"mastery": 0.31, "difficulty": 0.35, "last_error": "……", "memory": []},
  "review": [{"kc": "integral_definite", "due": "2026-11-17"}],
  "elapsed_ms": 42
}
```

六个动作：`start / submit / skip / hint / why / reset`；
`submit` 带 `answer`（空答传 `None` 或 `""`）；其余忽略 `answer`。

## 三、`serve.py` 补丁（20 行，注意 `ROOT` 必须是绝对路径）

```python
ALLOW_TUTOR = False
_TUTORS: dict[str, object] = {}

def _get_tutor(sid: str):
    from app.dataset_adapter import FileDataset
    from app.tutor.dialog import Tutor
    t = _TUTORS.get(sid)
    if t is None:
        pool = list(FileDataset(str(ROOT / "data")).common_dev(0))
        t = Tutor(sid=sid, pool=pool, root=str(ROOT))
        _TUTORS[sid] = t
    return t

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path == "/api/rollback":
            ...                      # 既有逻辑不动
        elif path == "/api/ask":
            self._handle_ask()
        else:
            self.send_error(404, "没有这个接口")

    def _handle_ask(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if not ALLOW_TUTOR:
                return self._json(501, {"error": "服务未开启实时（启动时加 --allow-tutor）"})
            res = _get_tutor(str(req.get("sid") or "")).handle(
                str(req.get("action") or ""), req.get("answer"))
            self._json(200, res)
        except Exception as e:  # noqa: BLE001 - 任何异常都不许让进程死
            self._json(500, {"ok": False, "error": f"Tutor: {e}"})
```

启动参数加一行 `ap.add_argument("--allow-tutor", action="store_true")`，并 `ALLOW_TUTOR = args.allow_tutor`。

## 四、验收（三条命令，10 秒出结论）

```bash
# 1) 起服务
python app/serve.py --port 8123 --allow-tutor

# 2) 契约探针（我写的，逐字段校验，不需要你写测试）
python docs/demo/probe_ask.py --url http://127.0.0.1:8123/api/ask
#    期望：RESULT: PASS（11 次请求，字段与状态机都符合契约 4.5）

# 3) 三条 curl 冒烟
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' -d '{"sid":"live01","action":"start"}'
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' -d '{"sid":"live01","action":"submit","answer":"0"}'
curl -s -X POST http://127.0.0.1:8123/api/ask -H 'Content-Type: application/json' -d '{"sid":"live01","action":"reset"}'
```

探针会自动检查：12 个字段齐全、`hint.level` 连错 5 次升到 4 封顶、`mastery` 三个知识点都在 0..1、
`err` 在枚举内、响应里没有 `reference`。**探针 PASS，前端就能直接吃。**

## 五、五个坑（每条都有前车之鉴）

1. **不要改 `web/`**：切真接口是 C 侧改一行 `LIVE.endpoint`，你改页面会和 C 冲突。
2. `serve.py` 启动会 `os.chdir(WEB)`，**所有路径用 `ROOT = Path(__file__).resolve().parents[1]` 绝对拼接**。
3. `hint` 的文案只能走 `hint.render()`，不要在 `dialog.py` 里硬写（否则五级梯度会散）。
4. **任何异常返回 `{"ok":false,"error":...}`（500）**，不要让进程死；前端会挂黄条并自动回放。
5. 单线程 `TCPServer`，**不要加锁**；状态按 `sid` 分，演示用 `demo`、实时用 `live01`，别互相覆盖。

## 六、如果只能交一半

优先 `start / submit / reset` 三个动作：前端就能"出题 → 判分 → 重置"，
`hint` 可以返回固定文本、`why` 返回 `mastery/difficulty/last_error` 三个数（memory 给 `[]`）。
探针会把缺的部分逐条列出来，照着补即可。
