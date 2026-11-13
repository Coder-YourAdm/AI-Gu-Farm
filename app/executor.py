"""判分内核 · 执行器。

权威契约：`方案/技术规格_v5.md` §2.3（v5.1）。
参数名、类型注解、返回值类型都不许改——B 和 C 都按这六个签名对接。

铁律：
1. 入口先拒绝，再计算。
2. 全程 fractions.Fraction，禁止 float。
3. 参考答案只能由本模块算出，不能相信任何模型输出。
4. solver_view() 是白名单，任何泄漏 reference 的写法都是错的。
"""

MAX_DIGITS = 64            # 分子/分母/整数的位数上限（规格 §2.4）
MAX_ANSWER_CHARS = 100     # {"value": "..."} 里字符串去空白后的长度上限
MAX_RESPONSE_CHARS = 8000  # 原始响应字符上限，超了在 json.loads 之前就拒（S1）


def validate_task(raw: dict) -> tuple[bool, str]:
    """校验出题对象是否符合模板 schema。

    返回 (ok, reason)。ok=False 时 reason 是拒绝原因，调用方按「本次尝试失败」处理。

    必须拒绝的情况：
    - 未知键；模板名不在 poly / gamma / gaussian_odd / det 之内
    - 用 bool / float / 字符串冒充 int（必须写 type(v) is int，
      isinstance(True, int) 是 True，光靠 isinstance 拦不住）
    - 参数越界：rate=0、c=0、n=0、coeffs 末项为 0、upper <= lower、非方阵、阶数不在 2..5
    """
    raise NotImplementedError("A1-1：validate_task")


def normalize_task(raw: dict, *, task_id: str, source: str, by: str | None, gen: int | None) -> dict:
    """把校验通过的出题对象补全成后端 Task。

    填 id / source / by / gen / type / template / params / signature / statement / reference。
    reference 只能来自 solve_exact()，不能来自模型 claim。
    """
    raise NotImplementedError("A1-2：normalize_task")


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
    raise NotImplementedError("A1-3：solve_exact")


def solver_view(task: dict) -> dict:
    """白名单构造给模型的 SolverTask。

    键集合必须恰为 {type, statement, answer_format}。
    不得出现 template / params / by / gen / signature / reference / 历史答题结果（S6）。
    """
    raise NotImplementedError("A1-4：solver_view")


def signature(task: dict) -> str:
    """规范化签名，用于硬去重。

    固定键序 + 规范参数；矩阵含完整元素，poly 含上下限。
    可以对规范 JSON 做 SHA-256，但数学等价性由规范化规则承担，不要求哈希解决。
    """
    raise NotImplementedError("A1-5：signature")


def judge(task: dict, raw_answer: str | None) -> str:
    """返回 "right" / "wrong" / "unanswered"。

    纯函数：不读文件、不读配置里的场景标签、不知道这道题是互答还是共同卷。
    格式错（含空、非法 JSON、非 {"value": "..."}）是 unanswered；
    数值合法但不等是 wrong；两者都计 0 且占分母。
    积分可比分数与有限十进制（1e-8 相对容差），行列式只接整数。
    """
    raise NotImplementedError("A1-6：judge")
