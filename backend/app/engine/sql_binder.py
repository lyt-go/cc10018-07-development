"""SQL模板参数绑定引擎：支持占位符解析、类型校验、默认值回填、动态SQL生成。

支持的占位符语法：
- {{param_name}}        → 文本替换（自动转义）
- {{param_name:int}}    → 整数类型校验
- {{param_name:date}}   → 日期格式校验
- {{param_name:list}}   → IN 列表展开
- {{#if cond}}...{{/if}} → 条件分支
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.store import store


class SqlTemplateBinder:
    """SQL模板参数绑定引擎。"""

    TYPE_PATTERNS = {
        "int": r"^-?\d+$",
        "float": r"^-?\d+(\.\d+)?$",
        "date": r"^\d{4}-\d{2}-\d{2}$",
        "datetime": r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$",
        "str": r".*",
    }

    def __init__(self) -> None:
        pass

    def parse_params(self, sql_text: str) -> list[dict[str, str]]:
        """从SQL模板中提取所有参数标记。"""
        pattern = r"\{\{(\w+)(?::(\w+))?\}\}"
        found = []
        seen = set()
        for m in re.finditer(pattern, sql_text):
            name, ptype = m.group(1), m.group(2) or "str"
            if name not in seen:
                seen.add(name)
                found.append({"name": name, "type": ptype, "pos": m.start()})
        return found

    def validate_params(self, sql_text: str, values: dict[str, Any]) -> list[str]:
        """校验参数值是否符合模板声明的类型。"""
        errors = []
        params = self.parse_params(sql_text)
        for p in params:
            name = p["name"]
            ptype = p["type"]
            val = values.get(name)
            if val is None:
                # 尝试从模板默认配置中补全
                tmpl = self._find_template_by_sql(sql_text)
                if tmpl:
                    defaults = self._parse_defaults(tmpl.get("默认值", ""))
                    val = defaults.get(name)
                if val is None:
                    errors.append(f"参数 {name} 未提供")
                    continue
            val_str = str(val)
            pat = self.TYPE_PATTERNS.get(ptype, self.TYPE_PATTERNS["str"])
            if not re.match(pat, val_str):
                errors.append(f"参数 {name} 值 '{val_str}' 不符合类型 {ptype}")
        return errors

    def bind(self, sql_text: str, values: dict[str, Any]) -> dict[str, Any]:
        """执行参数绑定，返回绑定后的SQL和参数列表。"""
        errors = self.validate_params(sql_text, values)
        if errors:
            return {"ok": False, "errors": errors, "sql": None, "params": []}

        bound_sql = sql_text
        bound_params: list[Any] = []

        # 处理列表类型展开
        def replace_list(m: re.Match) -> str:
            name = m.group(1)
            val = values.get(name, [])
            if isinstance(val, str):
                val = [v.strip() for v in val.split(",")]
            placeholders = ", ".join(f"${len(bound_params) + j + 1}" for j in range(len(val)))
            bound_params.extend(val)
            return placeholders

        # 先处理 :list 类型
        bound_sql = re.sub(r"\{\{(\w+):list\}\}", replace_list, bound_sql)

        # 再处理普通类型
        def replace_param(m: re.Match) -> str:
            name = m.group(1)
            ptype = m.group(2) or "str"
            val = values.get(name)
            if val is None:
                tmpl = self._find_template_by_sql(sql_text)
                if tmpl:
                    defaults = self._parse_defaults(tmpl.get("默认值", ""))
                    val = defaults.get(name)
            if ptype == "int":
                val = int(val)  # type: ignore[arg-type]
            elif ptype == "float":
                val = float(val)  # type: ignore[arg-type]
            bound_params.append(val)
            return f"${len(bound_params)}"

        bound_sql = re.sub(r"\{\{(\w+)(?::(\w+))?\}\}", replace_param, bound_sql)

        return {"ok": True, "sql": bound_sql, "params": bound_params}

    def expand_template(self, template_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按模板编号加载并展开。"""
        tmpl = store.find("sql_template", template_id)
        if not tmpl:
            return {"ok": False, "errors": [f"模板 {template_id} 不存在"], "sql": None}
        sql_text = tmpl.get("SQL片段", "")
        return self.bind(sql_text, values)

    def expand_api_query(self, api_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按API接口编号加载SQL并展开（API服务→已发布SQL契约包→查询执行链）。

        只读取接口SQL契约包里当前生效的那一版：SQL、参数声明、返回字段都来自同一份
        不可变快照，避免参数和返回列读到不同版本。
        """
        from app.engine.sql_contract import contract_registry

        api = store.find("api_service", api_id)
        if not api:
            return {"ok": False, "errors": [f"接口 {api_id} 不存在"], "sql": None}
        api_code = str(api.get("接口编号") or "")
        contract = contract_registry.pin(api_code)
        if contract is None:
            return {
                "ok": False,
                "errors": [f"接口 {api_code} 尚无已发布的SQL契约包，不能执行绑定"],
                "sql": None,
                "params": [],
                "contract": None,
            }
        return self.bind_contract(contract, values)

    def bind_contract(self, contract: Any, values: dict[str, Any]) -> dict[str, Any]:
        """按契约版本声明的参数类型绑定，并返回该版契约的返回字段顺序。"""
        errors: list[str] = []
        resolved: dict[str, Any] = {}
        for spec in contract.params:
            val = values.get(spec.name)
            if val is None or (isinstance(val, str) and val == ""):
                val = spec.default
            if val is None:
                if spec.required:
                    errors.append(f"参数 {spec.name} 未提供")
                continue
            resolved[spec.name] = val
        extra = sorted(set(values) - {spec.name for spec in contract.params})
        if extra:
            errors.append(f"参数 {', '.join(extra)} 不在契约声明中")

        # SQL 里出现但契约没有声明的占位符同样不能被静默放过。
        declared = {spec.name for spec in contract.params}
        for param in self.parse_params(contract.sql):
            if param["name"] not in declared:
                errors.append(f"SQL占位符 {param['name']} 未在契约参数声明中")

        if errors:
            return {
                "ok": False,
                "errors": errors,
                "sql": None,
                "params": [],
                "return_fields": list(contract.return_fields),
                "contract": self._contract_meta(contract),
            }

        # 绑定前按契约声明的类型做一次校验。
        type_errors: list[str] = []
        for spec in contract.params:
            if spec.name not in resolved:
                continue
            val_str = str(resolved[spec.name])
            if spec.type == "list":
                continue
            pattern = self.TYPE_PATTERNS.get(spec.type, self.TYPE_PATTERNS["str"])
            if not re.match(pattern, val_str):
                type_errors.append(f"参数 {spec.name} 值 '{val_str}' 不符合类型 {spec.type}")
        if type_errors:
            return {
                "ok": False,
                "errors": type_errors,
                "sql": None,
                "params": [],
                "return_fields": list(contract.return_fields),
                "contract": self._contract_meta(contract),
            }

        spec_by_name = {spec.name: spec for spec in contract.params}
        bound_params: list[Any] = []

        def replace_list(m: re.Match) -> str:
            name = m.group(1)
            val = resolved.get(name, [])
            if isinstance(val, str):
                val = [v.strip() for v in val.split(",") if v.strip()]
            placeholders = ", ".join(f"${len(bound_params) + j + 1}" for j in range(len(val)))
            bound_params.extend(val)
            return placeholders

        bound_sql = re.sub(r"\{\{(\w+):list\}\}", replace_list, contract.sql)

        def replace_param(m: re.Match) -> str:
            name = m.group(1)
            spec = spec_by_name.get(name)
            ptype = spec.type if spec else (m.group(2) or "str")
            val = resolved.get(name)
            if ptype == "int":
                val = int(val)  # type: ignore[arg-type]
            elif ptype == "float":
                val = float(val)  # type: ignore[arg-type]
            bound_params.append(val)
            return f"${len(bound_params)}"

        bound_sql = re.sub(r"\{\{(\w+)(?::(\w+))?\}\}", replace_param, bound_sql)

        return {
            "ok": True,
            "errors": [],
            "sql": bound_sql,
            "params": bound_params,
            "bindings": {name: resolved[name] for name in resolved},
            "return_fields": list(contract.return_fields),
            "contract": self._contract_meta(contract),
        }

    @staticmethod
    def _contract_meta(contract: Any) -> dict[str, Any]:
        return {
            "api_code": contract.api_code,
            "version_no": contract.version_no,
            "version_label": contract.version_label,
            "template_ref": contract.template_ref,
            "template_version": contract.template_version,
            "checksum": contract.checksum,
            "params": [spec.as_dict() for spec in contract.params],
            "return_fields": list(contract.return_fields),
        }

    def _find_template_by_sql(self, sql_text: str) -> dict[str, Any] | None:
        for r in store.rows("sql_template"):
            if r.get("SQL片段", "") == sql_text:
                return r
        return None

    def _parse_defaults(self, defaults_raw: str) -> dict[str, str]:
        """解析默认值为字典，如 'start_date=2026-01-01,end_date=2026-12-31'。"""
        result = {}
        for part in defaults_raw.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def suggest_optimization(self, sql_text: str) -> list[dict[str, str]]:
        """基于SQL文本给出优化建议（与查询优化模块联动）。"""
        suggestions = []
        sql_upper = sql_text.upper()

        if "SELECT *" in sql_upper:
            suggestions.append({
                "type": "column_prune",
                "severity": "warning",
                "message": "避免使用 SELECT *，应显式列出所需字段以减少IO",
            })

        if re.search(r"\bLIKE\s+['\"]%[^'\"]+%['\"]", sql_text, re.IGNORECASE):
            suggestions.append({
                "type": "index_unfriendly",
                "severity": "warning",
                "message": "前后通配LIKE会导致索引失效，建议考虑全文检索或前缀索引",
            })

        if sql_upper.count("JOIN") > 3:
            suggestions.append({
                "type": "join_complexity",
                "severity": "info",
                "message": f"检测到 {sql_upper.count('JOIN')} 个JOIN，建议检查是否可拆分为子查询或物化视图",
            })

        if "ORDER BY" in sql_upper and "LIMIT" not in sql_upper:
            suggestions.append({
                "type": "sort_cost",
                "severity": "info",
                "message": "无LIMIT的ORDER BY可能导致全表排序，建议添加分页限制",
            })

        if re.search(r"\bWHERE\b", sql_upper) and not re.search(r"\bINDEX\b", sql_upper):
            suggestions.append({
                "type": "index_suggest",
                "severity": "info",
                "message": "WHERE条件字段建议创建索引以加速查询",
            })

        return suggestions
