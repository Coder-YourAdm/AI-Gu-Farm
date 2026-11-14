"""pilot / 正式运行前的离线体检，默认不发任何网络请求。

    python tools/preflight.py                        # 体检 config.local.json（没有则 config.dev.json）
    python tools/preflight.py --config config.dev.json
    python tools/preflight.py --probe                # 额外真发一次最小请求，验证 base_url / 鉴权 / model ID

退出码：0 = 没有 FAIL（可以有 WARN）；1 = 至少一条 FAIL。

为什么要有这个脚本：判分链路、数据、编排器各有各的测试，但「现在这份 config 拿到钥匙之后
能不能直接开跑」这件事没人管。常见翻车点全在这里：model 还是占位符、没设 wall_limit_s 或
max_calls（require_runtime_ready 会拒）、data/ 与 executor 不是同一版、runs/ 没写权限、
盘只剩几个 G 跑一半写不进快照。

--probe 会花一次极小的真实请求（几个 token）。除此之外本脚本全程离线。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    key: str
    status: str
    title: str
    detail: str = ""

    @property
    def icon(self) -> str:
        return {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[self.status]


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------

def check_config(path: Path) -> tuple[Check, object | None]:
    from app.config import Config, ConfigError

    if not path.exists():
        return Check("config", FAIL, "配置文件存在", f"{path} 不存在；先 cp config.dev.json config.local.json"), None
    try:
        cfg = Config.load(path)
    except ConfigError as exc:
        return Check("config", FAIL, "配置结构与语义", f"{type(exc).__name__}: {exc}"), None
    return Check("config", OK, "配置结构与语义", f"{path.name}｜condition={cfg.condition}"
                f"｜n_pop={cfg.n_pop} n_gen={cfg.n_gen}｜指纹 {cfg.semantic_fingerprint()[:12]}…"), cfg


def check_runtime_ready(cfg) -> Check:
    from app.config import MissingConfigError

    try:
        cfg.require_runtime_ready()
    except MissingConfigError as exc:
        return Check("runtime_ready", FAIL, "真实运行前置闸 require_runtime_ready", str(exc))
    return Check("runtime_ready", OK, "真实运行前置闸 require_runtime_ready",
                 f"model={cfg.model}｜base_url={cfg.base_url}")


def check_secret(cfg, *, require_key: bool) -> Check:
    env_name = cfg.api_key_env
    value = os.environ.get(env_name, "")
    if value.strip():
        return Check("secret", OK, "API 密钥环境变量", f"${env_name} 已设置（长度 {len(value)}，不显示内容）")
    status = FAIL if require_key else WARN
    return Check("secret", status, "API 密钥环境变量",
                 f"${env_name} 未设置；导出后重跑，或用 --require-key 让它变成硬失败")


def check_data(data_dir: Path) -> Check:
    from app.dataset_adapter import FileDataset

    try:
        ds = FileDataset(data_dir)
    except Exception as exc:  # noqa: BLE001 - 体检脚本要把任何原因都摊出来
        return Check("data", FAIL, "数据集装载与自校验", f"{type(exc).__name__}: {exc}")

    sigs = len(ds.signatures())
    groups = sorted(ds._common)
    detail = (f"{data_dir}｜签名 {sigs} 个互斥｜共同卷 {len(groups)} 组 × "
              f"{len(ds._common[groups[0]])} 题｜挑战池 {len(ds.challenge_pool())}｜"
              f"holdout 指纹 {ds.holdout_digest[:12]}…")
    if groups != list(range(1, 21)):
        return Check("data", WARN, "数据集装载与自校验", detail + f"｜⚠ 共同卷代数标签是 {groups[:3]}…，引擎按 1..20 对齐")
    return Check("data", OK, "数据集装载与自校验", detail)


def check_executor() -> Check:
    """拿附录 A 的四个已知答案走一遍 judge，确认判分链路自洽。"""
    from app import executor

    cases = [
        ({"template": "poly", "params": {"coeffs": [3, -2, 1], "lower": -1, "upper": 2}}, "9"),
        ({"template": "poly", "params": {"coeffs": [0, 0, 5], "lower": 1, "upper": 2}}, "35/3"),
        ({"template": "gaussian_odd", "params": {"k": 5, "rate": 1, "c": -2}}, "-120"),
        ({"template": "det", "params": {"matrix": [[0, 2, 1], [3, 0, 1], [4, 5, 0]]}}, "23"),
    ]
    for raw, expected in cases:
        ok, reason = executor.validate_task(raw)
        if not ok:
            return Check("executor", FAIL, "判分链路往返", f"{raw['template']} 校验失败：{reason}")
        task = executor.normalize_task(raw, task_id="PF", source="preflight", by=None, gen=None)
        ref = task["reference"]
        got = ref["value"] if ref["kind"] == "integer" else f"{ref['numerator']}/{ref['denominator']}"
        if got != expected:
            return Check("executor", FAIL, "判分链路往返",
                         f"{raw['template']} 参考值 {got} ≠ 期望 {expected}")
        if executor.judge(task, json.dumps({"value": expected})) != "right":
            return Check("executor", FAIL, "判分链路往返", f"{raw['template']} 正确值未被判 right")
        if executor.judge(task, json.dumps({"value": "0" if expected != "0" else "1"})) != "wrong":
            return Check("executor", FAIL, "判分链路往返", f"{raw['template']} 错误值未被判 wrong")
    return Check("executor", OK, "判分链路往返", "I1 / I3 / I7 / D7 四个已知答案：出题→判 right，错答→判 wrong")


def check_runs(runs_root: Path) -> Check:
    try:
        runs_root.mkdir(parents=True, exist_ok=True)
        probe = runs_root / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("runs", FAIL, "运行目录可写", f"{runs_root} 写不进去：{exc}")
    free_gb = shutil.disk_usage(runs_root).free / 1024 ** 3
    if free_gb < 2:
        return Check("runs", FAIL, "运行目录可写", f"{runs_root} 可写，但只剩 {free_gb:.1f} GB")
    if free_gb < 10:
        return Check("runs", WARN, "运行目录可写", f"{runs_root} 可写，剩余 {free_gb:.1f} GB，六趟跑完请留够")
    return Check("runs", OK, "运行目录可写", f"{runs_root} 可写，剩余 {free_gb:.1f} GB")


def check_deps() -> Check:
    try:
        import httpx
    except ImportError:
        return Check("deps", FAIL, "异步 HTTP 依赖", "缺 httpx；pip install -r requirements.txt")
    return Check("deps", OK, "异步 HTTP 依赖", f"httpx {httpx.__version__}")


def check_plan(cfg) -> Check:
    """按公式算调用量，把「跑得起但跑不完」提前说清楚。"""
    from app.config import call_plan

    try:
        plan = call_plan(cfg)
    except Exception as exc:  # noqa: BLE001
        return Check("plan", WARN, "调用量估算", f"{type(exc).__name__}: {exc}")
    per_run = plan[cfg.condition]
    detail = (f"{cfg.condition} 单趟 {per_run} 次调用（每代 {plan['per_gen']}"
              f"｜挑战 {plan['challenge_slots']}｜留出 {plan['holdout_slots']}"
              f"｜算子 {plan['operator_calls']}）")
    if cfg.max_calls is not None:
        ratio = per_run / cfg.max_calls if cfg.max_calls else float("inf")
        detail += f"｜max_calls={int(cfg.max_calls)}（占 {ratio:.0%}）"
        if ratio > 1:
            return Check("plan", FAIL, "调用量估算", detail + "｜上限比一趟所需还小，第 1 趟都跑不完")
        if ratio > 0.8:
            return Check("plan", WARN, "调用量估算", detail + "｜余量不足 20%，六趟合计会撞上限")
    if cfg.wall_limit_s is not None:
        detail += f"｜wall_limit={cfg.wall_limit_s}s"
    return Check("plan", OK, "调用量估算", detail)


def check_probe(cfg) -> Check:
    """真发一次最小请求。只验「地址通、钥匙对、model ID 存在」，不碰题目。"""
    try:
        import httpx
    except ImportError:
        return Check("probe", SKIP, "真实连通性探测", "缺 httpx，跳过")
    key = os.environ.get(cfg.api_key_env, "").strip()
    if not key:
        return Check("probe", SKIP, "真实连通性探测", f"${cfg.api_key_env} 未设置")

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {"model": cfg.model,
               "messages": [{"role": "user", "content": "Reply with the single character: 1"}],
               "max_tokens": 4, "temperature": 0}
    t0 = time.time()
    try:
        resp = httpx.post(url, headers={"Authorization": f"Bearer {key}",
                                        "Content-Type": "application/json"},
                          json=payload, timeout=min(cfg.timeout_s, 60))
    except Exception as exc:  # noqa: BLE001
        return Check("probe", FAIL, "真实连通性探测", f"{type(exc).__name__}: {exc}")
    elapsed = time.time() - t0
    if resp.status_code == 401:
        return Check("probe", FAIL, "真实连通性探测", "401：密钥无效或没带对前缀")
    if resp.status_code == 404:
        return Check("probe", FAIL, "真实连通性探测",
                     f"404：{url} 不存在，检查 base_url 是否已包含 /v1")
    if resp.status_code != 200:
        return Check("probe", FAIL, "真实连通性探测",
                     f"HTTP {resp.status_code}：{resp.text[:160]}")
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return Check("probe", FAIL, "真实连通性探测", f"响应不是 OpenAI 兼容格式：{type(exc).__name__}｜{resp.text[:160]}")
    usage = body.get("usage") or {}
    return Check("probe", OK, "真实连通性探测",
                 f"{elapsed:.2f}s｜model 回显 {body.get('model', '?')}｜回复 {content.strip()[:20]!r}"
                 f"｜tokens {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')}")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> int:
    """把 `.env` 里的 KEY=VALUE 放进 os.environ（不覆盖已有变量）。返回载入条数。

    只认最朴素的格式：`NAME=value`、`# 注释`、空行。不做引号解析、不做变量展开——
    这个文件是我们自己写的，不是通用 dotenv 实现。
    """
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name and name not in os.environ:
            os.environ[name] = value
            loaded += 1
    return loaded


def run_checks(config_path: Path, *, data_dir: Path, runs_root: Path,               require_key: bool = False, probe: bool = False) -> list[Check]:
    checks: list[Check] = []

    cfg_check, cfg = check_config(config_path)
    checks.append(cfg_check)
    if cfg is None:
        checks.append(Check("rest", SKIP, "其余检查", "配置没通过，后面的项无法进行"))
        return checks

    checks.append(check_runtime_ready(cfg))
    checks.append(check_secret(cfg, require_key=require_key))
    checks.append(check_deps())
    checks.append(check_plan(cfg))
    checks.append(check_data(data_dir))
    checks.append(check_executor())
    checks.append(check_runs(runs_root))
    checks.append(check_probe(cfg) if probe else
                  Check("probe", SKIP, "真实连通性探测", "未加 --probe；拿钥匙后再跑一次"))
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pilot 前置体检（默认离线）")
    ap.add_argument("--config", default=None, help="默认优先 config.local.json，其次 config.dev.json")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--require-key", action="store_true", help="密钥缺失视为 FAIL")
    ap.add_argument("--probe", action="store_true", help="额外发一次最小真实请求")
    args = ap.parse_args(argv)

    # `.env` 不会自动进环境变量。不处理它，就会反复出现
    # 「我明明把 key 写进 .env 了，preflight 还说没设」——这一步替人做掉。
    _load_dotenv(ROOT / ".env")

    if args.config:
        config_path = Path(args.config)
    else:
        local = ROOT / "config.local.json"
        config_path = local if local.exists() else ROOT / "config.dev.json"
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    checks = run_checks(config_path, data_dir=ROOT / args.data_dir,
                        runs_root=ROOT / args.runs_root,
                        require_key=args.require_key, probe=args.probe)

    print("=" * 74)
    print("preflight｜pilot 前置体检（除 --probe 外全程离线，不发模型请求）")
    print("=" * 74)
    for c in checks:
        print(f"{c.icon} {c.title}")
        if c.detail:
            print(f"       {c.detail}")
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    print("-" * 74)
    print(f"结果：{len(checks)} 项｜FAIL {len(fails)}｜WARN {len(warns)}")
    if fails:
        print("阻断项：")
        for c in fails:
            print(f"  - {c.title}：{c.detail}")
        return 1
    print("可以开跑。" if not warns else "可以开跑，但先看上面的 WARN。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
