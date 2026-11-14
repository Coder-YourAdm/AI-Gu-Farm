#!/usr/bin/env bash
# 正式 E/F 各 3 趟，一条命令跑完 —— **B 引擎（app/run.py）唯一驱动**
#
# 与上一版（跑 app/evolve_run.py）的差别，改之前先读：
#   1. 主循环统一到 app/run.py。C 的 evolve_run.py 不再驱动正式实验，
#      只作为 --freeze/--resume 的参考实现留档与交叉验证。
#   2. **E 与 F 的差异只落在 configs/{E,F}.json 的 condition 一个字段。**
#      B 的 semantic_fingerprint 把 condition 算进去，E/F 的指纹必然不同，
#      所以它证明不了「除条件外零差异」。脚本末尾改用更直接、评委也看得懂的
#      证据：去掉 condition 之后，两份配置必须逐字节相同。
#   3. 每代 78 次调用（setter 6 + 互答 30 + 共同卷 24 + 祖先 3×6）。
#      祖先评测是结构参数（app/config.py 的 _STRUCTURAL 锁死 n_ancestor=3），
#      关不掉。一趟 E 理论 2289 次、F 2232 次，比旧实现（每代 36 次）贵约 3 倍。
#   4. **不能给 --pilot 配 --gens**：main() 里 `if args.pilot: raw['challenge_gens']=[]`，
#      带 --gens 会把八代/十五代的挑战注入静默清空。代数只从 config 的 n_gen 取。
#   5. 续跑是**派生新分支**（--resume X 会生成 X_demo_<nanos>），原始趟不可变。
#      B 的设计如此：证据不许被覆盖。所以续跑后的目录名与原始趟不同，汇总要一并认。
#   6. 留出评测仍用 app/evaluate_holdout.py：B 的 history.jsonl 里
#      evaluated_population / ranking / genes 字段与之兼容，页面直接吃，
#      且 --out 落在各趟目录，不写 web/（写 web/ 会互相覆盖）。
#
# 用法：
#   bash tools/run_official.sh                 # 跑全部 6 趟 + 评测
#   bash tools/run_official.sh --only E1       # 只跑某一趟
#   bash tools/run_official.sh --no-eval       # 只跑演化，不做留出
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

ONLY=""
DO_EVAL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --only)    ONLY="$2"; shift 2 ;;
    --no-eval) DO_EVAL=0; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

# 密钥：优先环境变量，其次仓库外的 secrets 文件
if [ -z "${LLM_API_KEY:-}" ]; then
  SEC=/d/WorkBuddy-temp/secrets/deepseek.key
  if [ -f "$SEC" ]; then
    LLM_API_KEY="$(tr -d '\r\n' < "$SEC")"
    export LLM_API_KEY
  else
    echo "✗ 没有 LLM_API_KEY，也不在 $SEC" >&2
    exit 2
  fi
fi

LOG_DIR=/d/WorkBuddy-temp/outputs
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/official_$(date +%Y%m%d_%H%M%S).log"
echo "日志: $LOG"

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

RAN=0

# 一趟 = 演化 + 留出评测
run_one() {
  local rid="$1"; local cond="$2"
  # --only 允许写 E1 或 EVO_E1 两种形式。只认前者会静默跳过整趟，
  # 日志里只剩一行「全部结束」，看起来像跑成功了 —— 这种静默失败最费时间。
  if [ -n "$ONLY" ] && [ "$ONLY" != "$rid" ] && [ "$ONLY" != "${rid#EVO_}" ]; then
    return 0
  fi
  RAN=$((RAN + 1))

  say "──── $rid 开始（condition=$cond，引擎 app/run.py）"

  # 已完成则跳过：B 写完一趟会落 summaries/<run_id>.json
  if [ -f "runs/summaries/$rid.json" ]; then
    say "  ↷ $rid 已有完成汇总，跳过（要重跑请先移走 runs/snapshots/$rid）"
  else
    # 有快照但无汇总 = 上次中断，用 B 的续跑（派生新分支，原始趟不动）
    local args="--run --pilot --config configs/$cond.json --run-id $rid --runs-root runs"
    if [ -d "runs/snapshots/$rid" ] && [ -n "$(ls -A "runs/snapshots/$rid" 2>/dev/null)" ]; then
      args="--resume $rid --runs-root runs --branch ${rid}_cont"
      say "  ↻ 检测到未完成的快照，按 B 的口径派生续跑分支 ${rid}_cont"
    fi

    python -m app.run $args 2>&1 | tee -a "$LOG" | tail -6
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
      say "✗ $rid 演化失败 rc=$rc，跳过本趟"
      return 1
    fi
  fi

  if [ "$DO_EVAL" = "1" ]; then
    # 先把根 history.jsonl 里本趟的记录拆出来：export 与留出评测都只按 gen
    # 去重、不看 run_id，直接读混装文件会让六趟互相覆盖同代记录。
    say "$rid 拆分 history 并做留出评测"
    python tools/split_history.py --run-id "$rid" 2>&1 | tee -a "$LOG" | tail -2
    python -m app.evaluate_holdout --history "runs/snapshots/$rid/history.jsonl" \
      --out "runs/snapshots/$rid/result.json" 2>&1 | tee -a "$LOG" | tail -6
  fi
  say "──── $rid 结束"
}

for i in 1 2 3; do run_one "EVO_E$i" "E" || true; done
for i in 1 2 3; do run_one "EVO_F$i" "F" || true; done

if [ "$RAN" -eq 0 ]; then
  say "✗ 一趟都没跑：--only=${ONLY:-（空）} 没有匹配任何 run_id（可选 E1/E2/E3/F1/F2/F3）"
  exit 3
fi

say "全部结束。汇总："
python - <<'PY' 2>&1 | tee -a "$LOG"
import hashlib, json, pathlib

# ① 证据一：E/F 两份配置除 condition 外必须逐字节相同
a = json.loads(pathlib.Path("configs/E.json").read_text(encoding="utf-8"))
b = json.loads(pathlib.Path("configs/F.json").read_text(encoding="utf-8"))
a.pop("condition", None); b.pop("condition", None)
sa = json.dumps(a, ensure_ascii=False, sort_keys=True, indent=1)
sb = json.dumps(b, ensure_ascii=False, sort_keys=True, indent=1)
if sa == sb:
    print(f"✓ E/F 配置去掉 condition 后逐字节相同"
          f"（fingerprint {hashlib.sha256(sa.encode()).hexdigest()[:16]}）"
          f"——除条件外无任何参数差异")
else:
    print("✗ E/F 配置除 condition 外还有别的差异，这份对照不能当同一实验比较")

# ② 证据二：每趟的完成代数与调用数
rows = []
for p in sorted(pathlib.Path("runs/summaries").glob("EVO_*.json")):
    m = json.loads(p.read_text(encoding="utf-8"))
    rows.append((p.stem, m.get("config", {}).get("condition"),
                 m.get("completed"), m.get("aborted"), m.get("call_total")))
for r in rows:
    print(f"  {r[0]:10s} cond={r[1]} 完成{r[2]}代 中止{r[3]} 调用{r[4]}")
if not rows:
    print("  （还没有完成的趟）")
PY
