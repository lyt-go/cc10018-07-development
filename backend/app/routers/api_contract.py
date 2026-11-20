"""接口SQL契约包接口：查询已发布契约、启动/校验/中断/发布，以及执行一次查询。

一次查询要么整份使用同一个已发布版本，要么直接失败；不存在“参数读新版、返回列读旧版”
的中间态。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.engine.sql_binder import SqlTemplateBinder
from app.engine.sql_contract import ContractError, contract_registry, shape_rows
from app.services.sql_contract import SqlContractService

router = APIRouter(prefix="/api/api_contract", tags=["接口SQL契约包"])

service = SqlContractService()
binder = SqlTemplateBinder()


@router.get("/packages")
def list_packages() -> dict[str, Any]:
    """列出所有接口当前生效的契约包摘要。"""
    items = service.package_summaries()
    return {"items": items, "total": len(items)}


@router.get("/packages/{api_id}")
def get_active_package(api_id: int) -> dict[str, Any]:
    """读取接口当前生效的契约版本（SQL、参数声明、返回字段同属一版）。"""
    package = service.active_package(api_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"接口 {api_id} 尚无已发布的SQL契约包")
    return package


@router.get("/packages/{api_id}/versions")
def list_versions(api_id: int) -> dict[str, Any]:
    """读取接口的契约版本历史，最近的在前。"""
    versions = service.versions(api_id)
    return {"items": list(reversed(versions)), "total": len(versions)}


@router.post("/packages/{api_id}/validate")
def start_validation(api_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """启动发布前校验：对 SQL、参数类型、返回字段一起确认。"""
    try:
        session = service.start(api_id, payload or {})
    except ContractError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return session.as_dict()


@router.post("/validations/{token}/check")
def run_validation(token: str) -> dict[str, Any]:
    """执行校验；只有状态为“已校验”的会话才能进入发布。"""
    try:
        session = service.validate(token)
    except ContractError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return session.as_dict()


@router.post("/validations/{token}/abort")
def abort_validation(token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """中断校验：丢弃候选版本，旧版调用继续正常取数。"""
    try:
        session = service.abort(token)
    except ContractError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return {**session.as_dict(), "ok": True, "message": "校验已中断，生效版本保持不变"}


@router.post("/validations/{token}/publish")
def publish_validation(token: str) -> dict[str, Any]:
    """发布已校验契约；模板在校验期间被更正或校验不完整时拒绝发布。"""
    try:
        snapshot = service.publish(token)
    except ContractError as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, "errors": exc.errors})
    return {**snapshot.as_dict(), "ok": True, "message": f"契约版本 {snapshot.version_label} 已发布"}


@router.post("/packages/{api_id}/precheck")
def precheck(api_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """发布前预检：给出 SQL、参数、返回字段的完整结论，但不落版本。"""
    return service.precheck(api_id, payload or {})


@router.post("/packages/{api_id}/query")
def run_contract_query(api_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """用本次固定的契约版本绑定参数并整形结果。"""
    data = payload or {}
    api = service.resolve_api(api_id)
    if api is None:
        raise HTTPException(status_code=404, detail=f"接口 {api_id} 不存在")
    contract = contract_registry.pin(str(api.get("接口编号") or ""))
    if contract is None:
        raise HTTPException(status_code=409, detail=f"接口 {api_id} 尚无已发布的SQL契约包")
    bind_result = binder.bind_contract(contract, data.get("params") or {})
    return {
        **bind_result,
        "rows": shape_rows(contract, data.get("rows")) if data.get("rows") is not None else [],
    }
