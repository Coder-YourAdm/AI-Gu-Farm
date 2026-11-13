"""AI 养蛊机 · 应用包。

模块划分见 `方案/技术规格_v5.md` §1；接口以该文档为准，字段名逐字照抄。

分工：
  B 负责: config / llm / genotype / orchestrator / store / evaluate / run
  A 负责: executor / scoring / ancestor
  C 负责: datasets / prompts / challenge / export
"""

__version__ = "0.1.0"
