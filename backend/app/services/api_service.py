"""API服务业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.sql_contract import SqlContractService
from app.store import store

MODULE = "api_service"
LIST_FIELDS = ["接口编号", "接口名称", "查询SQL", "参数列表", "返回字段", "缓存时长", "限流阈值", "接口版本"]
REQUIRED_FIELDS = ["接口编号", "接口名称", "查询SQL"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
NEGATIVE_ACTIONS = ["下线接口"]


class ApiServiceService:
    def __init__(self) -> None:
        self.contracts = SqlContractService()

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("接口编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _needs_contract(self, entry: dict[str, Any]) -> bool:
        """没有关联 SQL 模板的接口不涉及契约包，沿用原发布流程。"""
        template_ref = str(entry.get("参数列表") or "").strip()
        return template_ref.startswith("SQL-") or any(
            row.get("模板编号") == template_ref for row in store.rows("sql_template"))

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据接口 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于API服务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        contract_note = ""
        if action == "发布接口" and self._needs_contract(entry):
            # 发布前校验：SQL、参数类型、返回字段必须来自同一版已校验契约，
            # 校验不通过时接口状态保持原样，生效版本也不动。
            snapshot, contract_note = self.contracts.validate_and_publish_entry(entry)
            if snapshot is None:
                return None, f"发布前校验未通过，接口未发布：{contract_note}"

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"数据接口已{action}"
        if contract_note:
            message = f"{message}；{contract_note}"
        return entry, message
