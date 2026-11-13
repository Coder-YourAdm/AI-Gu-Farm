"""判分内核 · 执行器。

权威契约：`方案/技术规格_v5.md` §2.3（v5.1）。
参数名、类型注解、返回值类型都不许改——B 和 C 都按这六个签名对接。

铁律：
1. 入口先拒绝，再计算。
2. 全程 fractions.Fraction，禁止 float。
3. 参考答案只能由本模块算出，不能相信任何模型输出。
4. solver_view() 是白名单，任何泄漏 reference 的写法都是错的。
"""

import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from math import factorial

MAX_DIGITS = 64            # 分子/分母/整数的位数上限（规格 §2.4）
MAX_ANSWER_CHARS = 100     # {"value": "..."} 里字符串去空白后的长度上限
MAX_RESPONSE_CHARS = 8000  # 原始响应字符上限，超了在 json.loads 之前就拒（S1）
MAX_EXPONENT = 30          # 十进制指数绝对值上限（规格 §2.4）

# ---------------------------------------------------------------------------
# 答案解析（规格 §2.4）
#
# 识别顺序固定：integer -> fraction -> decimal，三条都必须 fullmatch。
# 所有词法检查（长度 / 位数 / 指数）放在 int / Fraction / Decimal 之前——
# 先做完词法，再转换，避免被 "1e999999" 这种构造喂出一个天文数字的大整数。
# 全程不出现 float：Decimal 只负责把有限十进制精确转成 Fraction。
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"[+-]?[0-9]+")
_FRAC_RE = re.compile(r"([+-]?[0-9]+)/([0-9]+)")
_DEC_RE = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")


def parse_integer(s: str) -> int:
    """整数字面量 -> int。正则 `[+-]?[0-9]+`，位数 <= 64。

    bool 的防御不在这里：字符串 "1" 永远解析不出 True。
    真正要防的是 JSON 里的 `true` 被 json.loads 变成 Python True 之后
    冒充参数——那一步必须在 validate_task 里写 `type(v) is int`。
    """
    if not isinstance(s, str):
        raise ValueError("整数必须从字符串解析，收到 %s" % type(s).__name__)
    if not _INT_RE.fullmatch(s):
        raise ValueError("不是合法整数：%r" % s)
    if len(s.lstrip("+-")) > MAX_DIGITS:
        raise ValueError("整数位数超过 %d" % MAX_DIGITS)
    return int(s)


def parse_fraction(s: str) -> Fraction:
    """`a/b` -> Fraction（自动约分、分母恒正）。分母为 0 直接拒。

    只接 `[+-]?[0-9]+/[0-9]+`；纯整数请走 parse_integer。
    三条分支互斥，判分口径才唯一（规格 §2.4）。
    """
    if not isinstance(s, str):
        raise ValueError("分数必须从字符串解析，收到 %s" % type(s).__name__)
    m = _FRAC_RE.fullmatch(s)
    if not m:
        raise ValueError("不是合法分数：%r" % s)
    num_s, den_s = m.group(1), m.group(2)
    if len(num_s.lstrip("+-")) > MAX_DIGITS or len(den_s) > MAX_DIGITS:
        raise ValueError("分子或分母位数超过 %d" % MAX_DIGITS)
    den = int(den_s)
    if den == 0:
        raise ValueError("分母为 0：%r" % s)
    return Fraction(int(num_s), den)


def parse_decimal(s: str) -> Fraction:
    """有限十进制 -> Fraction。必须实际含小数点或 e/E，指数绝对值 <= 30。

    `"5"` 会先被 integer 分支接走；走到这里还不带小数点或指数的，
    说明调用方顺序错了，直接拒掉而不是猜。
    """
    if not isinstance(s, str):
        raise ValueError("十进制必须从字符串解析，收到 %s" % type(s).__name__)
    if not _DEC_RE.fullmatch(s):
        raise ValueError("不是合法有限十进制：%r" % s)
    if "." not in s and "e" not in s and "E" not in s:
        raise ValueError("纯整数请走 integer 分支：%r" % s)
    mantissa, exp = _split_exponent(s)
    if len(mantissa.lstrip("+-").replace(".", "")) > MAX_DIGITS:
        raise ValueError("尾数位数超过 %d" % MAX_DIGITS)
    if exp is not None and abs(exp) > MAX_EXPONENT:
        raise ValueError("指数绝对值超过 %d：%r" % (MAX_EXPONENT, s))
    try:
        return Fraction(Decimal(s))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("十进制无法精确转换：%r" % s) from exc


def _split_exponent(s: str) -> tuple[str, int | None]:
    """`1.25e-3` -> ("1.25", -3)；不含指数时 -> (s, None)。"""
    for i, ch in enumerate(s):
        if ch in "eE":
            return s[:i], int(s[i + 1:])
    return s, None


# ---------------------------------------------------------------------------
# 题面生成（规格 §2.5）
#
# 题面必须由执行器确定性生成：同输入永远同输出，且不出现模板名、rate/c/n
# 这类变量名，也不写「用阶乘公式」。24 条用例里至少 4 条断言题面逐字相等。
# ---------------------------------------------------------------------------

_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def _sup(n: int) -> str:
    """正整数 -> Unicode 上标；超过 19 回退成 ^n。"""
    if n > 19:
        return "^%d" % n
    return "".join(_SUP[int(d)] for d in str(n))


def _poly_expr(coeffs: list[int]) -> str:
    """多项式表达式：降幂、系数 1 省略、-1 只留负号、指数 1 不写上标。"""
    terms = [(a, i) for i, a in enumerate(coeffs) if a != 0]
    out = ""
    for idx, (a, i) in enumerate(reversed(terms)):
        neg = a < 0
        mag = abs(a)
        coef = "" if (mag == 1 and i > 0) else str(mag)
        if i == 0:
            var = ""
        elif i == 1:
            var = "x"
        else:
            var = "x" + _sup(i)
        piece = coef + var
        if idx == 0:
            out += ("-" if neg else "") + piece
        else:
            out += (" - " if neg else " + ") + piece
    return out


def statement(template: str, params: dict) -> str:
    """模板 + 参数 -> 题面字符串（纯函数，同输入同输出）。"""
    if template == "poly":
        return "计算定积分 ∫_{%d}^{%d} (%s) dx。" % (
            params["lower"], params["upper"], _poly_expr(params["coeffs"]))

    if template == "gamma":
        c, n, rate = params["c"], params["n"], params["rate"]
        coef = "" if c == 1 else ("-" if c == -1 else str(c))
        var = "x" if n == 1 else "x" + _sup(n)
        return "计算定积分 ∫₀^∞ %s%s e^{-%dx} dx。" % (coef, var, rate)

    if template == "gaussian_odd":
        c, k, rate = params["c"], params["k"], params["rate"]
        exp = 2 * k + 1
        coef = "" if c == 1 else ("-" if c == -1 else str(c))
        var = "x" if exp == 1 else "x" + _sup(exp)
        return "计算定积分 ∫₀^∞ %s%s e^{-%dx²} dx。" % (coef, var, rate)

    if template == "det":
        m = params["matrix"]
        rows = ", ".join("[" + ", ".join(str(v) for v in row) + "]" for row in m)
        return "计算下列 %d 阶矩阵的行列式：[%s]。" % (len(m), rows)

    raise ValueError("unknown template: %r" % (template,))


_TEMPLATES = ("poly", "gamma", "gaussian_odd", "det")


def _as_int(value, field: str, lo: int, hi: int) -> int:
    """严格整数校验：bool 也非法。

    `type(True) is int` 为 False，所以这一行同时挡住 bool、float、字符串。
    只写 isinstance(v, int) 会放过 True——那是 JSON 里 `true` 最常见的伪装。
    """
    if type(value) is not int:
        raise ValueError("%s 必须是 int，收到 %s" % (field, type(value).__name__))
    if not (lo <= value <= hi):
        raise ValueError("%s 越界：%r 不在 [%d, %d]" % (field, value, lo, hi))
    return value


def _check_params(template: str, params: dict) -> None:
    """按模板逐项校验参数，越界一律抛 ValueError。"""
    if template == "poly":
        if set(params) != {"coeffs", "lower", "upper"}:
            raise ValueError("poly 参数键必须恰为 coeffs/lower/upper，收到 %s" % sorted(params))
        coeffs = params["coeffs"]
        if not isinstance(coeffs, list) or not (2 <= len(coeffs) <= 5):
            raise ValueError("coeffs 必须是长度 2..5 的列表")
        for v in coeffs:
            _as_int(v, "coeffs 元素", -5, 5)
        if coeffs[-1] == 0:
            raise ValueError("coeffs 最高次系数不能为 0")
        if not any(c != 0 for c in coeffs[1:]):
            raise ValueError("至少需要一个 i>=1 的非零系数")
        lower = _as_int(params["lower"], "lower", -3, 3)
        _as_int(params["upper"], "upper", lower + 1, lower + 6)
        return

    if template == "gamma":
        if set(params) != {"c", "n", "rate"}:
            raise ValueError("gamma 参数键必须恰为 c/n/rate，收到 %s" % sorted(params))
        _as_int(params["n"], "n", 1, 9)
        _as_int(params["rate"], "rate", 1, 8)
        _as_int(params["c"], "c", -5, 5)
        if params["c"] == 0:
            raise ValueError("c 不能为 0（答案恒为 0，无区分度）")
        return

    if template == "gaussian_odd":
        if set(params) != {"c", "k", "rate"}:
            raise ValueError("gaussian_odd 参数键必须恰为 c/k/rate，收到 %s" % sorted(params))
        _as_int(params["k"], "k", 0, 5)
        _as_int(params["rate"], "rate", 1, 8)
        _as_int(params["c"], "c", -5, 5)
        if params["c"] == 0:
            raise ValueError("c 不能为 0（答案恒为 0，无区分度）")
        return

    if set(params) != {"matrix"}:
        raise ValueError("det 参数键必须恰为 matrix，收到 %s" % sorted(params))
    m = params["matrix"]
    if not isinstance(m, list) or not (2 <= len(m) <= 5):
        raise ValueError("矩阵必须是 2..5 行的列表")
    for row in m:
        if not isinstance(row, list) or len(row) != len(m):
            raise ValueError("矩阵必须是方阵")
        for v in row:
            _as_int(v, "矩阵元素", -9, 9)


def validate_task(raw: dict) -> tuple[bool, str]:
    """校验出题对象是否符合模板 schema。

    返回 (ok, reason)。ok=False 时 reason 是拒绝原因，调用方按「本次尝试失败」处理。
    这里只做 schema 与边界，不做数学求解——拒绝要在求值之前完成。
    """
    if not isinstance(raw, dict):
        return False, "出题对象必须是 JSON 对象"
    if set(raw) != {"template", "params"}:
        return False, "出题对象只能有 template 与 params，收到 %s" % sorted(raw)
    template = raw["template"]
    if template not in _TEMPLATES:
        return False, "未知模板：%r" % (template,)
    params = raw["params"]
    if not isinstance(params, dict):
        return False, "params 必须是对象"
    try:
        _check_params(template, params)
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def normalize_task(raw: dict, *, task_id: str, source: str, by: str | None, gen: int | None) -> dict:
    """把校验通过的出题对象补全成后端 Task。

    填 id / source / by / gen / type / template / params / signature / statement / reference。
    reference 只能来自 solve_exact()，不能来自模型 claim。
    """
    ok, reason = validate_task(raw)
    if not ok:
        raise ValueError("出题对象未通过校验：%s" % reason)
    template = raw["template"]
    params = raw["params"]
    return {
        "id": task_id,
        "source": source,
        "by": by,
        "gen": gen,
        "type": "det" if template == "det" else "integral",
        "template": template,
        "params": params,
        "signature": signature({"template": template, "params": params}),
        "statement": statement(template, params),
        "reference": solve_exact({"template": template, "params": params}),
    }


def bareiss(m: list[list[int]]) -> int:
    """Bareiss 精确消元，全程整数运算，不产生分数。

    主元为 0 就换行并翻转符号；某列全零则行列式为 0。
    `// prev` 是整数除法——整数矩阵在 Bareiss 下必然除尽，不会丢精度。
    """
    a = [row[:] for row in m]
    n, sign, prev = len(a), 1, 1
    for k in range(n - 1):
        if a[k][k] == 0:
            for i in range(k + 1, n):
                if a[i][k] != 0:
                    a[k], a[i] = a[i], a[k]
                    sign = -sign
                    break
            else:
                return 0
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                a[i][j] = (a[i][j] * a[k][k] - a[i][k] * a[k][j]) // prev
        prev = a[k][k]
    return sign * a[-1][-1]


def solve_exact(task: dict) -> dict:
    """仅接受已验证参数，返回可 JSON 化的 reference（精确值，不含浮点）。

    poly:           Σ aᵢ(u^(i+1) − l^(i+1))/(i+1)
    gamma:          c·n! / rate^(n+1)
    gaussian_odd:   c·k! / (2·rate^(k+1))
    det:            Bareiss 精确整数

    编码：分母为 1 时用 {"kind":"integer","value":"9"}，
          否则 {"kind":"rational","numerator":"3","denominator":"2"}；
          det 一律用 integer 形式。
    """
    t, p = task["template"], task["params"]
    if t == "poly":
        l, u = p["lower"], p["upper"]
        val = sum(
            Fraction(a * (u ** (i + 1) - l ** (i + 1)), i + 1)
            for i, a in enumerate(p["coeffs"])
        )
    elif t == "gamma":
        val = Fraction(p["c"] * factorial(p["n"]), p["rate"] ** (p["n"] + 1))
    elif t == "gaussian_odd":
        val = Fraction(p["c"] * factorial(p["k"]), 2 * p["rate"] ** (p["k"] + 1))
    elif t == "det":
        return {"kind": "integer", "value": str(bareiss(p["matrix"]))}
    else:
        raise ValueError("unknown template: %r" % (t,))

    if val.denominator == 1:
        return {"kind": "integer", "value": str(val.numerator)}
    return {
        "kind": "rational",
        "numerator": str(val.numerator),
        "denominator": str(val.denominator),
    }


_DECIMAL_TOL = Fraction(1, 10 ** 8)   # 大纲容差：1e-8 * max(1, |ref|)


def _ref_fraction(reference: dict) -> Fraction:
    """后端 reference -> Fraction。字符串形式保存，避免大整数被 JSON 变 float。

    结构非法时抛 ValueError：这属于数据/执行器故障，不是模型答错，
    上层应当中止该代而不是把它记成 wrong（规格 §15）。
    """
    if not isinstance(reference, dict):
        raise ValueError("reference 必须是对象，收到 %s" % type(reference).__name__)
    kind = reference.get("kind")
    if kind == "integer":
        return Fraction(int(reference["value"]))
    if kind == "rational":
        return Fraction(int(reference["numerator"]), int(reference["denominator"]))
    raise ValueError("reference.kind 非法：%r" % (kind,))


def solver_view(task: dict) -> dict:
    """白名单构造给模型的 SolverTask。

    键集合必须恰为 {type, statement, answer_format}。
    不得出现 template / params / by / gen / signature / reference / 历史答题结果（S6）。
    """
    answer_format = (
        "integer only" if task["type"] == "det"
        else "integer, fraction or finite decimal"
    )
    return {
        "type": task["type"],
        "statement": task["statement"],
        "answer_format": answer_format,
    }


def signature(task: dict) -> str:
    """规范化签名，用于硬去重。

    固定键序 + 规范参数；矩阵含完整元素，poly 含上下限。
    只保证本项目所定义形式的重复检测，不承担任意数学等价判定。
    """
    template = task["template"]
    params = task["params"]
    if template == "poly":
        body = "coeffs=%s|lower=%d|upper=%d" % (
            ",".join(str(v) for v in params["coeffs"]),
            params["lower"],
            params["upper"],
        )
    elif template == "det":
        body = "matrix=" + ";".join(
            ",".join(str(v) for v in row) for row in params["matrix"]
        )
    else:
        body = "|".join("%s=%d" % (k, params[k]) for k in sorted(params))
    return "%s|%s" % (template, body)


def judge(task: dict, raw_answer: str | None) -> str:
    """返回 "right" / "wrong" / "unanswered"。

    纯函数：不读文件、不读配置里的场景标签、不知道这道题是互答还是共同卷。
    格式错（含空、非法 JSON、非 {"value": "..."}）是 unanswered；
    数值合法但不等是 wrong；两者都计 0 且占分母。
    积分可比分数与有限十进制（1e-8 相对容差），行列式只接整数。
    """
    if not isinstance(raw_answer, str):
        return "unanswered"
    if len(raw_answer) > MAX_RESPONSE_CHARS:      # S1：超长在 json.loads 之前就拒
        return "unanswered"
    try:
        payload = json.loads(raw_answer)
    except (ValueError, TypeError):
        return "unanswered"
    if not isinstance(payload, dict) or set(payload) != {"value"}:
        return "unanswered"
    value = payload["value"]
    if not isinstance(value, str):
        return "unanswered"
    value = value.strip()
    if not value or len(value) > MAX_ANSWER_CHARS:
        return "unanswered"

    is_det = task["type"] == "det"
    used_decimal = False
    try:
        if is_det:
            answer = Fraction(parse_integer(value))
        else:
            try:
                answer = Fraction(parse_integer(value))
            except ValueError:
                try:
                    answer = parse_fraction(value)
                except ValueError:
                    answer = parse_decimal(value)
                    used_decimal = True
    except ValueError:
        return "unanswered"

    reference = _ref_fraction(task["reference"])
    if is_det:
        return "right" if answer == reference else "wrong"
    if used_decimal:
        tol = _DECIMAL_TOL * max(1, abs(reference))
        return "right" if abs(answer - reference) <= tol else "wrong"
    return "right" if answer == reference else "wrong"
