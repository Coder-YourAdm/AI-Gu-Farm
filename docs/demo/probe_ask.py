#!/usr/bin/env python3
"""B7 接口探针：对真实的 POST /api/ask 跑六个动作，逐字段校验契约 4.5。

为什么需要它：前端 `web/tutor.html` 只读契约里的固定字段，字段名/类型不对时
页面不会报错，只会"静默缺块"——肉眼很难发现。这个脚本把这件事变成一条命令。

用法：
    python docs/demo/probe_ask.py --url http://127.0.0.1:8123/api/ask
    python docs/demo/probe_ask.py --url ... --sid probe01
    python docs/demo/probe_ask.py --url ... --verbose     # 打印每次响应摘要

退出码：
    0 = 全部通过
    1 = 连上了，但响应不符合契约（会列出具体字段）
    2 = 连不上 / 接口不存在 / 服务没开 --allow-tutor
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Windows 控制台默认 GBK，中文会乱码；统一按 UTF-8 输出（配合 chcp 65001）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass

REQUIRED = ("ok", "turn", "kc", "verdict", "err", "statement",
            "answer_format", "hint", "mastery", "why", "review", "elapsed_ms")
ERR_ENUM = {"sign", "calc", "concept", "misread", "giveup", "none"}
KC_IDS = {"integral_definite", "integral_improper", "det"}
MAX_HINT = 4


def post(url: str, payload: dict, timeout: float):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def parse(raw: str):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 - 服务端回 HTML 时这里就是失败点
        return None


def check_shape(tag: str, obj, failures: list[str]) -> None:
    def bad(msg: str) -> None:
        failures.append(f"{tag}: {msg}")

    if not isinstance(obj, dict):
        bad("响应不是 JSON 对象")
        return
    for key in REQUIRED:
        if key not in obj:
            bad(f"缺字段 {key}")
    if obj.get("ok") is not True:
        bad(f"ok 不是 true（实际 {obj.get('ok')!r}）")
    if not isinstance(obj.get("turn"), int):
        bad("turn 不是整数")
    if obj.get("err") is not None and obj.get("err") not in ERR_ENUM:
        bad(f"err 不在枚举内：{obj.get('err')!r}")
    stmt = obj.get("statement")
    if not isinstance(stmt, str) or not stmt.strip():
        bad("statement 为空")
    if not isinstance(obj.get("answer_format"), str) or not obj.get("answer_format"):
        bad("answer_format 为空")

    hint = obj.get("hint")
    if not isinstance(hint, dict):
        bad("hint 不是对象")
    else:
        if not isinstance(hint.get("level"), int):
            bad("hint.level 不是整数")
        if not isinstance(hint.get("text"), str):
            bad("hint.text 不是字符串")

    mastery = obj.get("mastery")
    if not isinstance(mastery, dict):
        bad("mastery 不是对象")
    else:
        missing = KC_IDS - set(mastery)
        if missing:
            bad(f"mastery 缺知识点：{sorted(missing)}")
        for kc, val in mastery.items():
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                bad(f"mastery[{kc}] 不在 0..1：{val!r}")

    why = obj.get("why")
    if not isinstance(why, dict):
        bad("why 不是对象")
    else:
        for key in ("mastery", "difficulty", "last_error", "memory"):
            if key not in why:
                bad(f"why 缺 {key}")
        if not isinstance(why.get("memory"), list):
            bad("why.memory 不是数组")

    if not isinstance(obj.get("review"), list):
        bad("review 不是数组")
    if not isinstance(obj.get("elapsed_ms"), (int, float)):
        bad("elapsed_ms 不是数字")
    if "reference" in json.dumps(obj, ensure_ascii=False):
        bad("响应里出现 reference 字样（三硬标准）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="POST /api/ask 契约探针")
    ap.add_argument("--url", default="http://127.0.0.1:8123/api/ask")
    ap.add_argument("--sid", default="probe01", help="探针会话 id（别用 demo）")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    failures: list[str] = []
    warnings: list[str] = []
    seen: list[dict] = []

    def call(action: str, answer=None, step: str = "") -> dict | None:
        payload = {"sid": args.sid, "action": action}
        if answer is not None:
            payload["answer"] = answer
        label = step or action
        try:
            status, raw = post(args.url, payload, args.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 404:
                print(f"[X] {label}: HTTP 404 —— 这个地址没有 /api/ask（B7 还没实现？）")
            elif e.code == 501:
                print(f"[X] {label}: HTTP 501 —— 服务没开实时开关（启动时加 --allow-tutor）")
                print(f"    {body[:120]}")
            else:
                print(f"[X] {label}: HTTP {e.code} —— {body[:160]}")
            sys.exit(2)
        except Exception as e:  # noqa: BLE001 - 连不上就是连不上
            print(f"[X] 连不上 {args.url}：{type(e).__name__}: {e}")
            print("    先确认服务已起：python app/serve.py --port 8123 --allow-tutor")
            sys.exit(2)

        obj = parse(raw)
        if obj is None:
            print(f"[X] {label}: 响应不是 JSON（HTTP {status}），前 80 字：{raw[:80]!r}")
            sys.exit(2)
        if args.verbose:
            print(f"    {label}: turn={obj.get('turn')} "
                  f"verdict={obj.get('verdict')} err={obj.get('err')} "
                  f"hint={((obj.get('hint') or {}).get('level'))} "
                  f"stmt={(obj.get('statement') or '')[:24]}")
        seen.append(obj)
        check_shape(label, obj, failures)
        return obj

    print(f"探针目标：{args.url}　sid={args.sid}")
    print("== 1. start ==")
    start = call("start", step="start")
    if start and (start.get("hint") or {}).get("level") != 0:
        warnings.append(f"start 的 hint.level 不是 0：{(start.get('hint') or {}).get('level')}")
    if start and start.get("verdict") is not None:
        warnings.append(f"start 的 verdict 应为 null，实际 {start.get('verdict')!r}")
    first_stmt = (start or {}).get("statement")

    print("== 2. 连交 5 次错答案（期望 wrong + 提示升到 4 封顶）==")
    levels: list[int] = []
    for i in range(5):
        resp = call("submit", "0", step=f"submit-wrong-{i + 1}")
        lv = (resp.get("hint") or {}).get("level")
        levels.append(lv if isinstance(lv, int) else -1)
        if resp.get("verdict") != "wrong":
            failures.append(f"submit-wrong-{i + 1}: verdict 应为 wrong，实际 {resp.get('verdict')!r}")
        if resp.get("err") not in ERR_ENUM:
            failures.append(f"submit-wrong-{i + 1}: err 不在枚举内 {resp.get('err')!r}")
    if levels != sorted(levels):
        failures.append(f"提示级别不单调：{levels}")
    if levels and max(levels) > MAX_HINT:
        failures.append(f"提示级别超过 {MAX_HINT}：{levels}")
    if levels and levels[-1] != MAX_HINT:
        failures.append(f"连错 5 次后 hint.level 应封顶 {MAX_HINT}，实际 {levels[-1]}")
    print(f"    级别序列：{levels}")

    print("== 3. hint（再提示一点）==")
    before = (call("why", step="why-before-hint") or {})
    lv_before = (before.get("hint") or {}).get("level")
    after = call("hint", step="hint")
    lv_after = (after.get("hint") or {}).get("level")
    if isinstance(lv_before, int) and isinstance(lv_after, int) and lv_after < lv_before:
        failures.append(f"hint 让级别变小了：{lv_before} -> {lv_after}")
    if isinstance(lv_after, int) and lv_after > MAX_HINT:
        failures.append(f"hint 后级别超过 {MAX_HINT}：{lv_after}")

    print("== 4. why（为什么给我这道题）==")
    why_resp = call("why", step="why")
    why = (why_resp or {}).get("why") or {}
    for key in ("mastery", "difficulty"):
        if not isinstance(why.get(key), (int, float)):
            failures.append(f"why.{key} 不是数字：{why.get(key)!r}")
    if not isinstance(why.get("last_error"), str):
        failures.append("why.last_error 不是字符串")

    print("== 5. reset（回到首题）==")
    reset = call("reset", step="reset")
    if reset and first_stmt and reset.get("statement") != first_stmt:
        warnings.append("reset 后题面与 start 不同（若实现为『按掌握度重选首题』可忽略）")

    print("== 6. skip（我不会）==")
    skip = call("skip", step="skip")
    if skip and skip.get("verdict") not in ("wrong", None):
        warnings.append(f"skip 的 verdict 是 {skip.get('verdict')!r}（前端两种都能接受）")

    print()
    if failures:
        print(f"RESULT: FAIL（{len(failures)} 项契约问题）")
        for f in failures:
            print("  - " + f)
        if warnings:
            print("另有提醒：")
            for w in warnings:
                print("  ~ " + w)
        return 1
    print(f"RESULT: PASS（{len(seen)} 次请求，字段与状态机都符合契约 4.5）")
    for w in warnings:
        print("  ~ " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
