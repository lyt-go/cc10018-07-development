"""接口SQL契约包业务规则：校验会话、发布前确认与版本切换。

路由层只负责协议转换，所有“能不能发布 / 读哪一版”的判断都收在这里。
"""
from __future__ import annotations

from typing import Any

from app.engine.sql_contract import (
    SESSION_ABORTED,
    SESSION_FAILED,
    SESSION_PUBLISHED,
    SESSION_STALE,
    SESSION_VALIDATED,
    SESSION_VALIDATING,
    ContractError,
    ContractSnapshot,
    ValidationSession,
    contract_registry,
    validate_contract_fields,
)
from app.store import store

MODULE = "api_service"


class SqlContractService:
    """围绕接口SQL契约包的读取、校验与发布。"""

    def resolve_api(self, api_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, api_id)

    # ── 读取 ────────────────────────────────────
    def package_summaries(self) -> list[dict[str, Any]]:
        return contract_registry.list_packages()

    def active_package(self, api_id: int) -> dict[str, Any] | None:
        api = self.resolve_api(api_id)
        if api is None:
            return None
        contract = contract_registry.active(str(api.get("接口编号") or ""))
        return contract.as_dict() if contract else None

    def versions(self, api_id: int) -> list[dict[str, Any]]:
        api = self.resolve_api(api_id)
        if api is None:
            return []
        return [
            contract.as_dict()
            for contract in contract_registry.versions(str(api.get("接口编号") or ""))
        ]

    # ── 校验流程 ────────────────────────────────
    def start(self, api_id: int, overrides: dict[str, Any] | None = None) -> ValidationSession:
        api = self.resolve_api(api_id)
        if api is None:
            raise ContractError(f"接口 {api_id} 不存在")
        data = overrides or {}
        return contract_registry.start_validation(
            str(api.get("接口编号") or ""),
            entry=api,
            template_id=data.get("template_id"),
            sql=data.get("sql"),
            params=data.get("params"),
            return_fields=data.get("return_fields"),
            strict_projection=data.get("strict_projection"),
            version_label=data.get("version_label"),
            change_note=str(data.get("change_note") or ""),
        )

    def validate(self, token: str) -> ValidationSession:
        return contract_registry.validate(token)

    def abort(self, token: str) -> ValidationSession:
        return contract_registry.abort(token)

    def publish(self, token: str) -> ContractSnapshot:
        return contract_registry.publish(token)

    def session(self, token: str) -> ValidationSession | None:
        return contract_registry.session(token)

    # ── 与“发布接口”动作联动 ─────────────────────
    def validate_and_publish_entry(self, api: dict[str, Any]) -> tuple[ContractSnapshot | None, str]:
        """发布接口时同步发布一版契约；任何一步失败都不改变生效版本。"""
        api_code = str(api.get("接口编号") or "")
        try:
            session = contract_registry.start_validation(
                api_code,
                entry=api,
                change_note=f"接口 {api_code} 发布",
            )
        except ContractError as exc:
            return None, exc.message
        validated = contract_registry.validate(session.token)
        if validated.status != SESSION_VALIDATED:
            return None, "发布前校验未通过：" + "；".join(validated.errors)
        try:
            snapshot = contract_registry.publish(session.token)
        except ContractError as exc:
            return None, exc.message
        return snapshot, (
            f"契约版本 {snapshot.version_label} 已发布，"
            f"参数 {len(snapshot.params)} 个，返回字段 {', '.join(snapshot.return_fields)}"
        )

    # ── 发布前预检（不改状态） ────────────────────
    def precheck(self, api_id: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """在不落版本的前提下完整跑一遍校验，返回是否可发布。"""
        api = self.resolve_api(api_id)
        if api is None:
            return {"ok": False, "errors": [f"接口 {api_id} 不存在"]}
        try:
            session = self.start(api_id, overrides)
        except ContractError as exc:
            return {"ok": False, "errors": exc.errors}
        validated = self.validate(session.token)
        # 预检失败/成功都不改变生效版本，会话标记为中断以免被误发布。
        contract_registry.abort(session.token)
        return {
            "ok": validated.status == SESSION_VALIDATED,
            "api_code": str(api.get("接口编号") or ""),
            "status": "可发布" if validated.status == SESSION_VALIDATED else "不可发布",
            "sql": validated.sql,
            "params": [param.as_dict() for param in validated.params],
            "return_fields": list(validated.return_fields),
            "template_ref": validated.template_ref,
            "template_version": validated.template_version,
            "errors": list(validated.errors),
        }


# 状态常量再导出，路由层展示用
CONTRACT_STATUSES = (
    SESSION_VALIDATING,
    SESSION_VALIDATED,
    SESSION_FAILED,
    SESSION_ABORTED,
    SESSION_STALE,
    SESSION_PUBLISHED,
)

__all__ = [
    "SqlContractService",
    "CONTRACT_STATUSES",
    "validate_contract_fields",
]
