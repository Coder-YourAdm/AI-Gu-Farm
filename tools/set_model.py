#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令填好 config.local.json + .env，然后交给 preflight 去验证。

为什么要有它：
    `config.dev.json` 里 model / base_url 是占位符，`max_calls` 为空，
    直接 `--run` 会被 `require_runtime_ready` 拒。手工改 JSON 容易漏掉
    `max_calls`，也容易把 key 提交进仓库。这个脚本把这三件事一次做完：
    base_url 用预设、model 由你给、key 只写进 .env（已被 .gitignore 忽略）。

用法：
    python tools/set_model.py --list
    python tools/set_model.py --provider deepseek --model deepseek-chat --key sk-xxx
    python tools/set_model.py --provider ollama --model qwen2.5:14b
    python tools/set_model.py --provider deepseek --model deepseek-chat --key sk-xxx --dry-run

写完后的下一步（脚本也会打印）：
    set -a; source .env; set +a
    python tools/preflight.py --config config.local.json --require-key --probe
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 只预设 base_url；model 一律由使用者指定，避免写死一个会过期的 ID
PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek":  {"base_url": "https://api.deepseek.com/v1", "env": "LLM_API_KEY"},
    "moonshot":  {"base_url": "https://api.moonshot.cn/v1", "env": "LLM_API_KEY"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "env": "LLM_API_KEY"},
    "zhipu":     {"base_url": "https://open.bigmodel.cn/api/paas/v4", "env": "LLM_API_KEY"},
    "openai":    {"base_url": "https://api.openai.com/v1", "env": "LLM_API_KEY"},
    "ollama":    {"base_url": "http://127.0.0.1:11434/v1", "env": "LLM_API_KEY"},
}

NO_KEY_PROVIDERS = {"ollama"}
PLACEHOLDER_KEY = "ollama"          # Ollama 不校验 key，但不能为空
DEFAULT_MAX_CALLS = 3000            # E 一趟 2289，留 30% 余量


def build_config(base: dict, *, provider: str, model: str, base_url: str | None,
                 max_calls: int) -> dict:
    cfg = dict(base)
    cfg["model"] = model
    cfg["base_url"] = base_url or PROVIDERS[provider]["base_url"]
    cfg["api_key_env"] = PROVIDERS[provider]["env"]
    cfg["max_calls"] = max_calls
    # wall_limit_s 是必填键，不能删；有了 max_calls 之后把它显式置空
    cfg["wall_limit_s"] = None
    return cfg


def write_env(path: Path, key: str) -> bool:
    """把 key 写进 .env（存在则替换同名行）。返回是否新建了文件。"""
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("LLM_API_KEY="):
                lines.append(f"LLM_API_KEY={key}")
                replaced = True
            elif line.strip():
                lines.append(line)
    if not replaced:
        lines.append(f"LLM_API_KEY={key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return not replaced


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="填好 config.local.json 与 .env")
    ap.add_argument("--list", action="store_true", help="列出预设 provider 与 base_url")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), help="服务商")
    ap.add_argument("--model", help="模型 ID，必须与 preflight --probe 回显一致")
    ap.add_argument("--base-url", dest="base_url", default=None,
                    help="自定义 base_url（覆盖预设）")
    ap.add_argument("--key", default=None, help="API key；只写进 .env，不写进 JSON")
    ap.add_argument("--max-calls", dest="max_calls", type=int, default=DEFAULT_MAX_CALLS)
    ap.add_argument("--out-dir", default=".", help="输出目录，默认仓库根")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args(argv)

    if args.list:
        print("预设 provider（model 需自己指定）：")
        for name, meta in sorted(PROVIDERS.items()):
            flag = "（无需 key）" if name in NO_KEY_PROVIDERS else ""
            print(f"  {name:<10} {meta['base_url']:<52} {flag}")
        print("\n例：python tools/set_model.py --provider deepseek --model deepseek-chat --key sk-xxx")
        return 0

    if not args.provider or not args.model:
        print("缺参数：--provider 与 --model 必填（--list 看预设）", file=sys.stderr)
        return 2

    key = args.key
    if args.provider in NO_KEY_PROVIDERS:
        key = key or PLACEHOLDER_KEY
    elif not key:
        print(f"{args.provider} 需要 --key；只写进 .env，不会进 config.local.json", file=sys.stderr)
        return 2

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dev = ROOT / "config.dev.json"
    if not dev.exists():
        print(f"找不到 {dev}", file=sys.stderr)
        return 2

    cfg = build_config(json.loads(dev.read_text(encoding="utf-8")),
                       provider=args.provider, model=args.model,
                       base_url=args.base_url, max_calls=args.max_calls)

    # 先过一遍 Config，避免写出一份自己都跑不起来的配置
    from app.config import Config, MissingConfigError
    try:
        parsed = Config.from_dict(cfg)
        parsed.require_runtime_ready()
    except MissingConfigError as exc:
        print(f"配置仍不完整：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"配置非法：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    cfg_path = out / "config.local.json"
    env_path = out / ".env"
    masked = (key[:4] + "…" + key[-2:]) if key and len(key) > 8 else "（短 key 不回显）"

    if args.dry_run:
        print("[dry-run] 将写入", cfg_path)
        print(json.dumps({k: cfg[k] for k in ("model", "base_url", "api_key_env", "max_calls")},
                         ensure_ascii=False, indent=2))
        print(f"[dry-run] 将写入 {env_path}：LLM_API_KEY={masked}")
        return 0

    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    created = write_env(env_path, key)

    print(f"✓ 写入 {cfg_path}")
    print(f"  model      = {cfg['model']}")
    print(f"  base_url   = {cfg['base_url']}")
    print(f"  max_calls  = {cfg['max_calls']}（E 一趟 2289，占 {cfg['max_calls'] / 2289:.0%}）")
    print(f"✓ {'新建' if created else '更新'} {env_path}：LLM_API_KEY={masked}")
    print(f"  配置指纹   = {parsed.semantic_fingerprint()[:12]}…")
    print("\n下一步（两条命令，第二条会花几个 token）：")
    print("  set -a; source .env; set +a")
    print("  python tools/preflight.py --config config.local.json --require-key --probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
