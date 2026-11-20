"""接口SQL契约包测试：发布前校验、版本原子切换、中断恢复与返回列整形。"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.engine.chain_orchestrator import ChainOrchestrator
from app.engine.sql_binder import SqlTemplateBinder
from app.engine.sql_contract import (
    ContractError,
    ParamSpec,
    contract_registry,
    shape_rows,
    split_field_list,
    validate_contract_fields,
)
from app.seed import SEED_ROWS
from app.services.api_service import ApiServiceService
from app.services.sql_contract import SqlContractService
from app.store import store


def reset_world() -> None:
    """重建内存数据仓库和契约仓库，避免用例之间互相污染。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    contract_registry.reset()


@pytest.fixture(autouse=True)
def _fresh_world():
    reset_world()
    yield
    reset_world()


TRADE_PARAMS = {
    "start_date": "2026-11-01",
    "end_date": "2026-11-15",
    "shop_list": ["101", "102"],
}


def _template(template_code: str) -> dict:
    return next(r for r in store.rows("sql_template") if r.get("模板编号") == template_code)


def test_published_contract_read_by_binding_and_shaping():
    """绑定与结果整形都取自同一版已发布契约。"""
    binder = SqlTemplateBinder()
    contract = contract_registry.active("API-TRADE-001")
    assert contract is not None

    result = binder.bind_contract(contract, TRADE_PARAMS)
    assert result["ok"], result["errors"]
    assert result["contract"]["version_label"] == "v2"
    assert result["return_fields"] == ["dt", "shop_id", "gmv"]
    assert result["bindings"]["shop_list"] == ["101", "102"]
    # list 参数按契约展开成两个占位符，日期与列表值都在同一次绑定结果里
    assert sorted(str(v) for v in result["params"]) == ["101", "102", "2026-11-01", "2026-11-15"]
    assert "$1" in result["sql"] and "$4" in result["sql"]
    assert result["sql"].count("$") == 4

    rows = [
        {"gmv": 12, "dt": "2026-11-01", "shop_id": "101", "extra": "drop"},
        ["2026-11-02", "102", 20],
    ]
    shaped = shape_rows(contract, rows)
    assert shaped[0] == {"dt": "2026-11-01", "shop_id": "101", "gmv": 12}
    assert shaped[1] == {"dt": "2026-11-02", "shop_id": "102", "gmv": 20}
    # 返回字段顺序严格按契约声明，而不是按源行字典顺序
    assert list(shaped[0]) == ["dt", "shop_id", "gmv"]


def test_binding_rejects_params_outside_contract():
    binder = SqlTemplateBinder()
    contract = contract_registry.active("API-TRADE-001")
    result = binder.bind_contract(contract, {**TRADE_PARAMS, "unexpected": "x"})
    assert not result["ok"]
    assert any("unexpected" in e for e in result["errors"])

    bad_type = binder.bind_contract(contract, {**TRADE_PARAMS, "start_date": "11/01/2026"})
    assert not bad_type["ok"]
    assert any("不符合类型 date" in e for e in bad_type["errors"])


def test_draft_template_cannot_be_published():
    """草稿/待校验模板不允许进入发布结果。"""
    service = SqlContractService()
    api = store.find("api_service", 3)  # API-ITEM-001 → SQL-ITEM-001（待校验）
    assert _template("SQL-ITEM-001")["status"] == "待校验"

    precheck = service.precheck(3)
    assert precheck["ok"] is False
    assert any("状态为 待校验" in e for e in precheck["errors"])
    assert contract_registry.active("API-ITEM-001") is None

    entry, message = ApiServiceService().run_action(3, "发布接口")
    assert entry is None
    assert "发布前校验未通过" in message
    assert store.find("api_service", 3)["status"] == "待校验"
    assert contract_registry.active("API-ITEM-001") is None


def test_half_validated_contract_cannot_publish():
    """只启动校验、没跑完 check 的会话不能发布。"""
    service = SqlContractService()
    session = service.start(1)
    assert session.status == "校验中"
    with pytest.raises(ContractError) as excinfo:
        service.publish(session.token)
    assert "尚未完整校验" in str(excinfo.value)
    assert contract_registry.active("API-TRADE-001").version_no == 2


def test_validation_rejects_mismatched_sql_params_and_fields():
    """SQL、参数声明、返回字段任一对不上，整份契约校验失败。"""
    service = SqlContractService()
    session = service.start(1, {
        "params": "start_date:date,end_date:date",
        "return_fields": "dt,shop_id,gmv",
    })
    validated = service.validate(session.token)
    assert validated.status == "校验失败"
    assert any("shop_list" in e for e in validated.errors)
    with pytest.raises(ContractError):
        service.publish(session.token)

    session2 = service.start(1, {"return_fields": "dt,shop_id,not_a_column"})
    validated2 = service.validate(session2.token)
    assert validated2.status == "校验失败"
    assert any("not_a_column" in e for e in validated2.errors)


def test_validate_contract_fields_helper():
    sql = "SELECT a, SUM(b) AS total FROM t WHERE d = {{d:date}}"
    errors = validate_contract_fields(
        sql,
        [],
        ["a", "total"],
        strict_projection=True,
    )
    assert any("d 未在参数声明中" in e for e in errors)

    ok = validate_contract_fields(
        sql,
        [ParamSpec("d", "date", True, None)],
        ["a", "total"],
        strict_projection=False,
    )
    assert ok == []

    errors = validate_contract_fields(
        "SELECT a FROM t WHERE d = {{d:date}}",
        [],
        ["b"],
        strict_projection=True,
    )
    assert any("b 未出现在查询SQL的投影中" in e for e in errors)


def test_new_version_publishes_params_and_columns_together():
    """新版发布时参数声明和返回列必须一起生效。"""
    service = SqlContractService()
    binder = SqlTemplateBinder()

    old = contract_registry.active("API-TRADE-001")
    old_result = binder.bind_contract(old, TRADE_PARAMS)
    assert old_result["return_fields"] == ["dt", "shop_id", "gmv"]

    new_definition = (
        "SELECT dt, shop_id, SUM(order_amt) AS gross_amount, COUNT(*) AS order_cnt "
        "FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} AND region = {{region:str}} "
        "GROUP BY dt, shop_id ORDER BY dt"
    )
    session = service.start(1, {
        "sql": new_definition,
        "params": [
            {"name": "start_date", "type": "date", "required": True},
            {"name": "end_date", "type": "date", "required": True},
            {"name": "shop_list", "type": "list", "required": True},
            {"name": "region", "type": "str", "required": True},
        ],
        "return_fields": "dt,shop_id,gross_amount,order_cnt",
    })
    assert service.validate(session.token).status == "已校验"
    published = service.publish(session.token)

    assert published.version_no == 3
    assert [p.name for p in published.params] == ["start_date", "end_date", "shop_list", "region"]
    assert published.return_fields == ("dt", "shop_id", "gross_amount", "order_cnt")

    # 旧请求若仍按旧参数调用，不会被悄悄降级成新列；新参数缺失时绑定直接失败。
    stale_call = binder.bind_contract(published, TRADE_PARAMS)
    assert not stale_call["ok"]
    assert any("region 未提供" in e for e in stale_call["errors"])

    new_call = binder.bind_contract(published, {**TRADE_PARAMS, "region": "north"})
    assert new_call["ok"], new_call["errors"]
    assert new_call["return_fields"] == ["dt", "shop_id", "gross_amount", "order_cnt"]
    assert shape_rows(published, [{"dt": "d", "order_cnt": 3, "gmv": 9}]) == [
        {"dt": "d", "shop_id": None, "gross_amount": None, "order_cnt": 3}]

    # 同一个快照里参数和字段是同版的：不会出现新参数配旧列。
    assert new_call["contract"]["checksum"] == published.checksum
    assert contract_registry.active("API-TRADE-001").version_no == 3


def test_interruption_keeps_old_version_serving_then_retry_switches():
    """穿插中断与重试：中断期间旧版正常取数，恢复后新版参数和列一起生效。"""
    service = SqlContractService()
    orchestrator = ChainOrchestrator()
    binder = SqlTemplateBinder()

    new_sql = (
        "SELECT dt, shop_id, SUM(order_amt) AS gross_amount "
        "FROM dwd_trade_order_di "
        "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
        "AND shop_id IN {{shop_list:list}} AND channel = {{channel:str}} "
        "GROUP BY dt, shop_id ORDER BY dt"
    )
    overrides = {
        "sql": new_sql,
        "params": "start_date:date,end_date:date,shop_list:list,channel:str",
        "return_fields": "dt,shop_id,gross_amount",
    }

    # 1) 旧版正常调用
    old_contract = contract_registry.active("API-TRADE-001")
    old_chain = orchestrator.run_query_service_chain(
        1, 1, TRADE_PARAMS,
        result_rows=[{"dt": "2026-11-01", "shop_id": "101", "gmv": 7}],
    )
    assert old_chain["ok"], old_chain
    assert old_chain["contract"]["version_no"] == old_contract.version_no
    assert old_chain["return_fields"] == ["dt", "shop_id", "gmv"]

    # 2) 启动新版校验，旧版调用仍然照常
    session = service.start(1, overrides)
    mid_chain = orchestrator.run_query_service_chain(
        1, 1, TRADE_PARAMS,
        result_rows=[["2026-11-01", "101", 7]],
    )
    assert mid_chain["ok"]
    assert mid_chain["contract"]["version_no"] == old_contract.version_no
    assert mid_chain["return_fields"] == ["dt", "shop_id", "gmv"]
    assert mid_chain["rows"] == [{"dt": "2026-11-01", "shop_id": "101", "gmv": 7}]

    # 3) 中断校验：候选版本丢弃，旧版继续可用
    aborted = service.abort(session.token)
    assert aborted.status == "已中断"
    with pytest.raises(ContractError):
        service.publish(session.token)
    after_abort = orchestrator.run_query_service_chain(1, 1, TRADE_PARAMS)
    assert after_abort["ok"]
    assert after_abort["contract"]["version_no"] == old_contract.version_no
    assert contract_registry.active("API-TRADE-001") == old_contract

    # 4) 重试：重新校验并发布
    retry = service.start(1, overrides)
    assert service.validate(retry.token).status == "已校验"
    published = service.publish(retry.token)
    assert published.version_no == old_contract.version_no + 1
    assert published.return_fields == ("dt", "shop_id", "gross_amount")
    assert [p.name for p in published.params] == ["start_date", "end_date", "shop_list", "channel"]

    # 5) 新版生效：参数声明和返回列一起切换
    new_params = {**TRADE_PARAMS, "channel": "app"}
    new_chain = orchestrator.run_query_service_chain(
        1, 1, new_params,
        result_rows=[{"order_amt_sum": 1, "gross_amount": 88, "dt": "2026-11-02", "shop_id": "102"}],
    )
    assert new_chain["ok"], new_chain
    assert new_chain["contract"]["version_no"] == published.version_no
    assert new_chain["return_fields"] == ["dt", "shop_id", "gross_amount"]
    assert new_chain["rows"] == [{"dt": "2026-11-02", "shop_id": "102", "gross_amount": 88}]

    # 旧参数调用新版会被明确拒绝，而不是给出错位的结果
    stale = binder.bind_contract(published, TRADE_PARAMS)
    assert not stale["ok"]
    assert any("channel 未提供" in e for e in stale["errors"])


def test_template_correction_during_validation_blocks_publish():
    """校验期间模板被更正 → 本次校验失效，旧版保持生效。"""
    service = SqlContractService()
    old = contract_registry.active("API-TRADE-001")
    session = service.start(1, {"change_note": "模板参数更正"})
    assert service.validate(session.token).status == "已校验"

    template = _template("SQL-TRADE-001")
    template["默认值"] = "start_date=2026-11-02,end_date=2026-11-15,shop_list=101,102"
    template["模板版本"] = "v2.1"

    with pytest.raises(ContractError) as excinfo:
        service.publish(session.token)
    assert "校验期间被更正" in str(excinfo.value)
    assert service.session(session.token).status == "校验失效"
    assert contract_registry.active("API-TRADE-001") == old

    # 恢复后重新校验，新版参数与列一起生效
    template["默认值"] = "start_date=2026-11-01,end_date=2026-11-15,shop_list=101,102"
    retry = service.start(1, {"change_note": "模板参数更正"})
    assert service.validate(retry.token).status == "已校验"
    snapshot = service.publish(retry.token)
    assert snapshot.version_no == old.version_no + 1


def test_publish_entry_action_creates_contract_version():
    """发布接口动作走同一套预校验并落一版契约。"""
    service = ApiServiceService()
    entry, message = service.run_action(1, "发布接口")
    assert entry is not None, message
    assert entry["status"] == "已发布"
    assert "契约版本" in message
    active = contract_registry.active("API-TRADE-001")
    assert active.version_no == 3
    assert active.return_fields == ("dt", "shop_id", "gmv")


def test_http_contract_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    r = client.get("/api/api_contract/packages/1")
    assert r.status_code == 200
    assert r.json()["version_label"] == "v2"

    r = client.get("/api/api_contract/packages/3")
    assert r.status_code == 404

    r = client.post("/api/api_contract/packages/1/query", json={"params": TRADE_PARAMS})
    assert r.status_code == 200
    assert r.json()["return_fields"] == ["dt", "shop_id", "gmv"]

    started = client.post("/api/api_contract/packages/1/validate", json={}).json()
    token = started["validation_token"]
    r = client.post(f"/api/api_contract/validations/{token}/publish")
    assert r.status_code == 409

    r = client.post(f"/api/api_contract/validations/{token}/check")
    assert r.json()["status"] == "已校验"
    r = client.post(f"/api/api_contract/validations/{token}/publish")
    assert r.status_code == 200
    assert r.json()["version_no"] == 3

    r = client.get("/api/api_contract/packages/1/versions")
    assert [item["version_no"] for item in r.json()["items"]] == [3, 2]

    # 发布草稿模板接口应被拒绝且不改状态
    entry, message = ApiServiceService().run_action(3, "发布接口")
    assert entry is None
    r = client.get("/api/api_service/3")
    assert r.json()["status"] == "待校验"


def test_query_service_chain_requires_published_contract():
    """没有契约版本时不执行查询，而不是回落到模板文本。"""
    orchestrator = ChainOrchestrator()
    # API-LOGIS-001 的模板尚未发布，也没有契约版本
    result = orchestrator.run_query_service_chain(5, 4, {"start_date": "2026-11-01", "end_date": "2026-11-15"})
    assert result["ok"] is False
    assert any("SQL契约包" in step["detail"] for step in result["steps"])


def test_contract_shape_helpers():
    assert split_field_list("dt, shop_id , gmv") == ("dt", "shop_id", "gmv")
    assert split_field_list(["a", "b"]) == ("a", "b")


def test_single_request_never_reads_two_versions(monkeypatch):
    """一次请求进行中发布新版，本次请求仍只使用开始时固定的一版。

    通过在“查询优化”步骤里插入一次新版发布，模拟校验/发布与线上取数并发：
    请求前半段读到的 SQL 和后半段整形用的返回列必须同版。
    """
    service = SqlContractService()
    orchestrator = ChainOrchestrator()
    old = contract_registry.active("API-TRADE-001")

    def publish_new(_sql: str, *, api_id=None):
        retry = service.start(1, {
            "sql": (
                "SELECT dt, shop_id, SUM(order_amt) AS gross_amount "
                "FROM dwd_trade_order_di "
                "WHERE dt BETWEEN {{start_date:date}} AND {{end_date:date}} "
                "AND shop_id IN {{shop_list:list}} AND channel = {{channel:str}} "
                "GROUP BY dt, shop_id ORDER BY dt"
            ),
            "params": "start_date:date,end_date:date,shop_list:list,channel:str",
            "return_fields": "dt,shop_id,gross_amount",
        })
        service.validate(retry.token)
        service.publish(retry.token)
        return {"ok": True, "estimated_cost": 1, "index_suggestions": []}

    monkeypatch.setattr(orchestrator.optimizer, "analyze_sql", publish_new)

    result = orchestrator.run_query_service_chain(
        1, 1, TRADE_PARAMS,
        result_rows=[{"dt": "2026-11-01", "shop_id": "101", "gmv": 7, "gross_amount": 99}],
    )
    assert result["ok"], result
    # 请求内 SQL/参数/返回列都来自旧版
    assert result["contract"]["version_no"] == old.version_no
    assert result["return_fields"] == ["dt", "shop_id", "gmv"]
    assert result["rows"] == [{"dt": "2026-11-01", "shop_id": "101", "gmv": 7}]
    assert result["contract"]["checksum"] == old.checksum
    assert [p["name"] for p in result["contract"]["params"]] == ["start_date", "end_date", "shop_list"]
    # 请求结束后新版本才成为生效版本
    assert contract_registry.active("API-TRADE-001").version_no == old.version_no + 1
