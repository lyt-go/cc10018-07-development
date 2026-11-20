"""跨模块数据链路由：暴露10组真实数据链的端到端API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.engine.chain_orchestrator import ChainOrchestrator
from app.engine.metric_calculator import MetricCalculator
from app.engine.sql_binder import SqlTemplateBinder
from app.engine.query_optimizer import QueryOptimizer
from app.engine.export_sharder import ExportSharder

router = APIRouter(prefix="/api/chain", tags=["data-chain"])

orchestrator = ChainOrchestrator()


@router.get("/metric-compute")
def chain_metric_compute(domain_code: str, metric_code: str, aggregate_id: int) -> dict[str, Any]:
    """链路1: 指标计算链（数据源连接→指标域→派生指标→数据聚合）。"""
    result = orchestrator.run_metric_compute_chain(domain_code, metric_code, aggregate_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/query-service")
def chain_query_service(payload: dict[str, Any]) -> dict[str, Any]:
    """链路2: 查询服务链（API服务→SQL契约包→查询优化→数据预览）。"""
    api_id = payload.get("api_id")
    template_id = payload.get("template_id")
    params = payload.get("params", {})
    result = orchestrator.run_query_service_chain(
        api_id, template_id, params, result_rows=payload.get("rows"))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/consume-governance")
def chain_consume_governance(subscribe_id: int) -> dict[str, Any]:
    """链路3: 消费治理链（接口订阅→调用凭证→调用追踪→访问看板）。"""
    result = orchestrator.run_consume_governance_chain(subscribe_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/data-output")
def chain_data_output(export_id: int) -> dict[str, Any]:
    """链路4: 数据输出链（批量导出→数据清洗→结果缓存→查询审计）。"""
    result = orchestrator.run_data_output_chain(export_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/caliber-unify")
def chain_caliber_unify(homonym_id: int) -> dict[str, Any]:
    """链路5: 口径统一链（指标重名解析→口径注册）。"""
    result = orchestrator.run_caliber_unify_chain(homonym_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/version-manage")
def chain_version_manage(version_id: int) -> dict[str, Any]:
    """链路6: 版本管理链（接口版本→灰度发布）。"""
    result = orchestrator.run_version_manage_chain(version_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/api-lifecycle")
def chain_api_lifecycle(api_code: str) -> dict[str, Any]:
    """链路7: API生命周期链（发布→订阅→凭证→追踪→审计→看板）。"""
    result = orchestrator.run_api_lifecycle_chain(api_code)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/cache-warmup")
def chain_cache_warmup(api_id: int) -> dict[str, Any]:
    """链路8: 缓存预热链（API→模板→缓存配置→数据预览）。"""
    result = orchestrator.run_cache_warmup_chain(api_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/audit-trail")
def chain_audit_trail(trace_id: int) -> dict[str, Any]:
    """链路9: 审计追踪链（调用→追踪→审计→合规结论）。"""
    result = orchestrator.run_audit_trail_chain(trace_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/template-reuse")
def chain_template_reuse(template_id: int) -> dict[str, Any]:
    """链路10: 模板复用链（模板→API→优化→版本）。"""
    result = orchestrator.run_template_reuse_chain(template_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ───────────────────────────────────────────────
# 引擎级原子接口
# ───────────────────────────────────────────────

@router.get("/engine/compute-plan")
def engine_compute_plan(aggregate_id: int) -> dict[str, Any]:
    """生成聚合配置的计算计划。"""
    calc = MetricCalculator()
    plan = calc.build_compute_plan(aggregate_id)
    if not plan.get("ok"):
        raise HTTPException(status_code=400, detail=plan)
    return plan


@router.get("/engine/preview-sql")
def engine_preview_sql(aggregate_id: int) -> dict[str, Any]:
    """为聚合配置生成预览SQL。"""
    calc = MetricCalculator()
    sql = calc.preview_sql(aggregate_id)
    return {"sql": sql}


@router.post("/engine/bind-sql")
def engine_bind_sql(payload: dict[str, Any]) -> dict[str, Any]:
    """SQL模板参数绑定。"""
    binder = SqlTemplateBinder()
    sql_text = payload.get("sql", "")
    values = payload.get("values", {})
    result = binder.bind(sql_text, values)
    return result


@router.post("/engine/optimize-sql")
def engine_optimize_sql(payload: dict[str, Any]) -> dict[str, Any]:
    """分析SQL并生成优化建议。"""
    optimizer = QueryOptimizer()
    sql_text = payload.get("sql", "")
    api_id = payload.get("api_id")
    result = optimizer.analyze_sql(sql_text, api_id=api_id)
    return result


@router.get("/engine/export-shards")
def engine_export_shards(export_id: int) -> dict[str, Any]:
    """为导出任务生成分片计划。"""
    sharder = ExportSharder()
    result = sharder.generate_shards(export_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/engine/validate-metric-chain")
def engine_validate_metric_chain(domain_code: str, metric_code: str) -> dict[str, Any]:
    """校验指标域到派生指标的链路完整性。"""
    calc = MetricCalculator()
    result = calc.validate_metric_chain(domain_code, metric_code)
    return result
