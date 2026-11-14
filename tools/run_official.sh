#!/usr/bin/env bash
# 正式 E/F 各 3 趟 + 留出评测，一条命令跑完
#
# 设计约束（改之前先读）：
#   1. E 与 F 只允许差一个 --freeze 开关，config.json 全程不动。
#      这样两次运行的 config_sha256 必然相同 —— 哈希相等本身就是
#      「除条件外无任何参数差异」的机器可验证证据，比人在文档里写「没改过」强。
#   2. 每趟都带 --resume。中途网断/机器休眠后重跑本脚本，已完成的代会跳过，
#      不会从头再来一遍；也不会因为重跑而改变实验记录。
#   3. 留出评测的 --out 落在各自 run 目录，不写 web/。
#      六趟都往 web/result.json 写会互相覆盖，最后只剩一趟的数字。
#
# 用法：
#   bash tools/run_official.sh                 # 跑全部 6 趟 + 评测
#   bash tools/run_official.sh --only E1       # 只跑某一趟
#   bash tools/run_official.sh --no-eval       # 只跑演化，不做留出
#   bash tools/run_official.sh --gens 5        # 冒烟用，缩短代数
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

GENS=20
ONLY=""
DO_EVAL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --only)    ONLY="$2"; shift 2 ;;
    --gens)    GENS="$2"; shift 2 ;;
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
  local rid="$1"; local freeze_flag="$2"
  # --only 允许写 E1 或 OFF_E1 两种形式。只认前者会静默跳过整趟，
  # 日志里只会留一行「全部结束」，看起来像跑成功了 —— 这种静默失败最费时间。
  if [ -n "$ONLY" ] && [ "$ONLY" != "$rid" ] && [ "$ONLY" != "${rid#OFF_}" ]; then
    return 0
  fi
  RAN=$((RAN + 1))

  say "──── $rid 开始（freeze=${freeze_flag:-no}）"
  local resume_flag=""
  [ -f "runs/$rid/state.json" ] && resume_flag="--resume"

  # 演化。输出同时进日志，屏幕只留末几行。
  python -m app.evolve_run --run-id "$rid" --gens "$GENS" $freeze_flag $resume_flag 2>&1 \
    | tee -a "$LOG" | tail -4
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    say "✗ $rid 演化失败 rc=$rc，跳过本趟"
    return 1
  fi

  if [ "$DO_EVAL" = "1" ]; then
    say "$rid 留出评测开始"
    python -m app.evaluate_holdout --history "runs/$rid/history.jsonl" \
      --out "runs/$rid/result.json" 2>&1 | tee -a "$LOG" | tail -6
  fi
  say "──── $rid 结束"
}

for i in 1 2 3; do run_one "OFF_E$i" ""            || true; done
for i in 1 2 3; do run_one "OFF_F$i" "--freeze"    || true; done

if [ "$RAN" -eq 0 ]; then
  say "✗ 一趟都没跑：--only=${ONLY:-（空）} 没有匹配任何 run_id（可选 E1/E2/E3/F1/F2/F3）"
  exit 3
fi

say "全部结束。汇总："
python - <<'PY' 2>&1 | tee -a "$LOG"
import json, pathlib
rows = []
for p in sorted(pathlib.Path("runs").glob("OFF_*/manifest.json")):
    m = json.loads(p.read_text(encoding="utf-8"))
    hist = p.parent / "history.jsonl"
    gens = sum(1 for _ in hist.open(encoding="utf-8")) if hist.exists() else 0
    rows.append((p.parent.name, m.get("condition"), m.get("config_sha256", "")[:16],
                 m.get("freeze"), m.get("fitness_window"), gens))
for r in rows:
    print(f"  {r[0]:8s} cond={r[1]} sha={r[2]} freeze={r[3]} win={r[4]} 已完成{r[5]}代")

shas = {r[2] for r in rows}
print()
if len(shas) == 1:
    print(f"✓ 六趟 config_sha256 全部相同（{shas.pop()}）——除条件外无参数差异")
elif len(shas) > 1:
    print(f"✗ 出现 {len(shas)} 个不同 config 指纹：{shas}。这份对照不能当同一实验比较")
PY
