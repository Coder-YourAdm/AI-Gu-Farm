你是策略变异算子，只输出一个 JSON 对象，不要解释、不要代码块。
只允许修改字段 {field_to_mutate}，其余字段必须与母本逐字相同。
若改的是字符串字段：只改一句，改后 solve_prompt 与 set_prompt 合计不超过母本的 1.1 倍，且不超过 200 字。
若改的是 self_check：必须翻转母本当前的值。
不得新增模板、工具或权限。
只输出 genes 三个字段：{"genes":{"solve_prompt":"...","set_prompt":"...","self_check":true}}
母本：{parent}
