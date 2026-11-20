"""接口SQL契约包：把一次查询的 SQL、参数声明、返回字段冻结成不可变版本。

设计要点：
- 一个 ``ContractSnapshot`` 同时携带 SQL、参数声明（名称/类型/默认值）和按顺序排列的
  返回字段。三者来自同一个校验通过的版本，调用方一次取出后整条请求只使用这一份快照，
  因此不会出现“参数读到新版、返回列还是旧版”的撕裂。
- 契约版本只有在校验完整通过后才允许发布；发布时在锁内一次性切换 active 指针，
  新版本生效前始终由旧快照提供取数能力。
- 校验开始时会对关联模板做指纹快照，发布前重新读取模板；若校验期间模板被更正，
  本次校验结果判为失效，旧版本继续作为生效版本。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.engine.sql_binder import SqlTemplateBinder
from app.store import store

ALLOWED_PARAM_TYPES = ("int", "float", "date", "datetime", "list", "str")

SESSION_VALIDATING = "校验中"
SESSION_VALIDATED = "已校验"
SESSION_FAILED = "校验失败"
SESSION_ABORTED = "已中断"
SESSION_STALE = "校验失效"
SESSION_PUBLISHED = "已发布"


class ContractError(Exception):
    """契约校验或发布失败。"""

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or [message]


@dataclass(frozen=True)
class ParamSpec:
    """一个参数的声明：名称、类型、是否必填、默认值。"""

    name: str
    type: str = "str"
    required: bool = True
    default: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
        }


@dataclass(frozen=True)
class TemplateFingerprint:
    """校验开始时对模板做的不可变指纹。"""

    template_id: int
    status: str
    template_version: str
    sql: str
    param_markers: str
    param_types: str
    defaults: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "status": self.status,
            "template_version": self.template_version,
            "sql": self.sql,
            "param_markers": self.param_markers,
            "param_types": self.param_types,
            "defaults": self.defaults,
        }


@dataclass(frozen=True)
class ContractSnapshot:
    """已发布契约的不可变快照，一次请求只认这一份。"""

    api_code: str
    version_no: int
    version_label: str
    sql: str
    params: tuple[ParamSpec, ...]
    return_fields: tuple[str, ...]
    template_id: int | None
    template_ref: str
    template_version: str
    checksum: str
    validated_at: str
    published_at: str
    change_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_code": self.api_code,
            "version_no": self.version_no,
            "version_label": self.version_label,
            "status": "已发布",
            "sql": self.sql,
            "params": [param.as_dict() for param in self.params],
            "return_fields": list(self.return_fields),
            "template_id": self.template_id,
            "template_ref": self.template_ref,
            "template_version": self.template_version,
            "checksum": self.checksum,
            "validated_at": self.validated_at,
            "published_at": self.published_at,
            "change_note": self.change_note,
        }


@dataclass
class ValidationSession:
    """一次发布前校验的会话；未完整校验通过时不能发布。"""

    token: str
    api_code: str
    status: str
    sql: str
    params: tuple[ParamSpec, ...]
    return_fields: tuple[str, ...]
    template_id: int | None
    template_ref: str
    template_version: str
    fingerprint: TemplateFingerprint | None
    strict_projection: bool
    version_label: str | None
    change_note: str
    created_at: str
    errors: list[str] = field(default_factory=list)
    validated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.status in (SESSION_VALIDATED, SESSION_PUBLISHED),
            "validation_token": self.token,
            "api_code": self.api_code,
            "status": self.status,
            "sql": self.sql,
            "params": [param.as_dict() for param in self.params],
            "return_fields": list(self.return_fields),
            "template_id": self.template_id,
            "template_ref": self.template_ref,
            "template_version": self.template_version,
            "version_label": self.version_label,
            "errors": list(self.errors),
            "created_at": self.created_at,
            "validated_at": self.validated_at,
            "change_note": self.change_note,
        }


# ───────────────────────────────────────────────
# 字段解析与校验
# ───────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)(?::(\w+))?\}\}")


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分，忽略括号和引号内的逗号。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"', "`"):
            quote = char
            buf.append(char)
        elif char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf))
    return parts


def _unquote_identifier(value: str) -> str:
    return value.strip().strip("`\"[]")


def split_field_list(raw: Any) -> tuple[str, ...]:
    """把 "dt,shop_id,gmv" 或列表统一成有序字段元组。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(part.strip() for part in _split_top_level(raw))
    if isinstance(raw, Sequence):
        return tuple(str(part).strip() for part in raw)
    return (str(raw).strip(),)


def parse_param_declarations(raw: Any) -> tuple[ParamSpec, ...]:
    """解析参数声明，兼容列表、字典、字符串三种写法。"""
    if raw is None:
        return ()
    specs: list[ParamSpec] = []

    def add(name: str, ptype: str | None = None, required: bool = True, default: Any = None) -> None:
        specs.append(ParamSpec(
            name=name.strip(),
            type=(ptype or "str").strip() or "str",
            required=required,
            default=None if default is None else str(default),
        ))

    def add_text(text: str) -> None:
        for part in _split_top_level(text):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                name, ptype = part.split(":", 1)
                add(name, ptype)
            elif "=" in part:
                name, default = part.split("=", 1)
                add(name, "str", True, default)
            else:
                add(part)

    if isinstance(raw, str):
        add_text(raw)
    elif isinstance(raw, Mapping):
        for name, value in raw.items():
            if isinstance(value, Mapping):
                add(
                    str(name),
                    str(value.get("type") or value.get("参数类型") or "str"),
                    bool(value.get("required", True)),
                    value.get("default", value.get("默认值")),
                )
            else:
                add(str(name), str(value) if value is not None else "str")
    elif isinstance(raw, Sequence):
        for item in raw:
            if isinstance(item, Mapping):
                add(
                    str(item.get("name") or item.get("参数标记") or ""),
                    str(item.get("type") or item.get("参数类型") or "str"),
                    bool(item.get("required", True)),
                    item.get("default", item.get("默认值")),
                )
            elif isinstance(item, (list, tuple)) and item:
                add(
                    str(item[0]),
                    str(item[1]) if len(item) > 1 else "str",
                    bool(item[2]) if len(item) > 2 else True,
                    item[3] if len(item) > 3 else None,
                )
            else:
                add_text(str(item))
    return tuple(specs)


def parse_defaults(defaults_raw: Any) -> dict[str, str]:
    """把 "start_date=2026-11-01,end_date=2026-11-15" 解析成字典。"""
    result: dict[str, str] = {}
    if not isinstance(defaults_raw, str):
        return result
    for part in _split_top_level(defaults_raw):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def params_from_template(template: Mapping[str, Any]) -> tuple[ParamSpec, ...]:
    """从模板的 参数标记/参数类型/默认值 生成契约参数声明。"""
    marker_raw = str(template.get("参数标记") or "")
    names = [name.strip() for name in _split_top_level(marker_raw) if name.strip()]
    types_raw = str(template.get("参数类型") or "")
    type_items = [item.strip() for item in _split_top_level(types_raw) if item.strip()]
    defaults = parse_defaults(template.get("默认值") or "")
    if not names:
        # 模板没有单独维护参数标记时，直接从 SQL 占位符推导。
        binder = SqlTemplateBinder()
        placeholders = binder.parse_params(str(template.get("SQL片段") or ""))
        return tuple(
            ParamSpec(p["name"], p["type"], True, defaults.get(p["name"]))
            for p in placeholders
        )
    specs = []
    for index, name in enumerate(names):
        ptype = type_items[index] if index < len(type_items) else "str"
        specs.append(ParamSpec(name, ptype or "str", True, defaults.get(name)))
    return tuple(specs)


def params_from_sql(sql_text: str) -> tuple[ParamSpec, ...]:
    binder = SqlTemplateBinder()
    return tuple(
        ParamSpec(p["name"], p["type"], True, None)
        for p in binder.parse_params(sql_text)
    )


def extract_projection(sql_text: str) -> list[str] | None:
    """尽力从 SELECT ... FROM 中解析出投影列/别名。"""
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    names: list[str] = []
    for raw_item in _split_top_level(match.group(1)):
        item = raw_item.strip()
        if not item:
            continue
        alias = re.search(r"\bAS\s+([`\"\[]?[\w]+[`\"\]]?)\s*$", item, re.IGNORECASE)
        if alias:
            names.append(_unquote_identifier(alias.group(1)).lower())
            continue
        if re.fullmatch(r"[`\"\[]?[\w.]+[`\"\]]?", item):
            names.append(_unquote_identifier(item.split(".")[-1]).lower())
            continue
        trailing = re.match(r"^(.+?)\s+([`\"\[]?[\w]+[`\"\]]?)\s*$", item, re.DOTALL)
        if trailing and "(" in trailing.group(1):
            names.append(_unquote_identifier(trailing.group(2)).lower())
            continue
        # 无别名的复杂表达式，保留原文参与比对。
        names.append(item.lower())
    return names


def validate_contract_fields(
    sql_text: str,
    params: Sequence[ParamSpec],
    return_fields: Sequence[str],
    *,
    strict_projection: bool = True,
) -> list[str]:
    """校验 SQL、参数类型、返回字段能否构成一份自洽契约。"""
    errors: list[str] = []

    if not str(sql_text or "").strip():
        errors.append("查询SQL不能为空")
        sql_text = ""

    declared: dict[str, ParamSpec] = {}
    for spec in params:
        if not spec.name:
            errors.append("参数声明缺少名称")
            continue
        if spec.name in declared:
            errors.append(f"参数 {spec.name} 重复声明")
            continue
        if spec.type not in ALLOWED_PARAM_TYPES:
            errors.append(f"参数 {spec.name} 的类型 {spec.type} 不受支持")
        declared[spec.name] = spec

    placeholders = list(_PLACEHOLDER_RE.finditer(sql_text))
    placeholder_names = {m.group(1) for m in placeholders}
    for match in placeholders:
        name = match.group(1)
        annotated = match.group(2)
        if name not in declared:
            errors.append(f"SQL占位符 {name} 未在参数声明中")
            continue
        if annotated and annotated != declared[name].type:
            errors.append(
                f"参数 {name} 的声明类型 {declared[name].type} 与SQL标注 {annotated} 不一致")
    for name in declared:
        if name not in placeholder_names:
            errors.append(f"参数 {name} 在SQL中未被使用")

    if not return_fields:
        errors.append("返回字段不能为空")
    seen: set[str] = set()
    cleaned_fields: list[str] = []
    for name in return_fields:
        value = str(name).strip()
        if not value:
            errors.append("返回字段中存在空字段名")
            continue
        key = value.casefold()
        if key in seen:
            errors.append(f"返回字段 {value} 重复")
            continue
        seen.add(key)
        cleaned_fields.append(value)

    if strict_projection and sql_text:
        projection = extract_projection(sql_text)
        if projection is None:
            errors.append("查询SQL不是可解析的 SELECT ... FROM 结构，无法确认返回字段")
        else:
            available = {name.casefold() for name in projection}
            for name in cleaned_fields:
                if name.casefold() not in available:
                    errors.append(f"返回字段 {name} 未出现在查询SQL的投影中")

    return errors


def compute_checksum(
    sql_text: str,
    params: Sequence[ParamSpec],
    return_fields: Sequence[str],
) -> str:
    payload = {
        "sql": sql_text,
        "params": [param.as_dict() for param in params],
        "return_fields": list(return_fields),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:24]


def fingerprint_template(template: Mapping[str, Any]) -> TemplateFingerprint:
    return TemplateFingerprint(
        template_id=int(template.get("id", 0)),
        status=str(template.get("status", "")),
        template_version=str(template.get("模板版本", "")),
        sql=str(template.get("SQL片段", "")),
        param_markers=str(template.get("参数标记", "")),
        param_types=str(template.get("参数类型", "")),
        defaults=str(template.get("默认值", "")),
    )


def shape_rows(contract: ContractSnapshot, rows: Iterable[Any] | None) -> list[dict[str, Any]]:
    """按契约版本声明的字段顺序整形结果，丢弃契约外字段。"""
    shaped: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            shaped.append({name: row.get(name) for name in contract.return_fields})
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            shaped.append({
                name: row[index] if index < len(row) else None
                for index, name in enumerate(contract.return_fields)
            })
        else:
            shaped.append({name: None for name in contract.return_fields})
    return shaped


@dataclass
class _Candidate:
    sql: str
    params: tuple[ParamSpec, ...]
    return_fields: tuple[str, ...]
    template: dict[str, Any] | None
    template_ref: str
    strict_projection: bool
    errors: list[str]


def build_candidate(
    entry: Mapping[str, Any] | None,
    *,
    api_code: str,
    template_id: int | None = None,
    sql: str | None = None,
    params: Any = None,
    return_fields: Any = None,
    strict_projection: bool | None = None,
) -> _Candidate:
    """从接口/模板/显式入参构造一版候选契约（尚未校验）。"""
    errors: list[str] = []
    template: dict[str, Any] | None = None
    template_ref = str((entry or {}).get("参数列表") or "").strip()

    if template_id is not None:
        template = store.find("sql_template", template_id)
        if template is None:
            errors.append(f"模板 {template_id} 不存在")
    elif template_ref.startswith("SQL-"):
        template = next(
            (row for row in store.rows("sql_template") if row.get("模板编号") == template_ref),
            None,
        )
        if template is None:
            errors.append(f"关联模板 {template_ref} 不存在")

    if template is not None:
        template_ref = str(template.get("模板编号") or template_ref)
        if template.get("status") != "已生效":
            errors.append(
                f"关联模板 {template_ref} 状态为 {template.get('status') or '未知'}，草稿或待校验模板不能发布")
        resolved_sql = str(sql if sql is not None else template.get("SQL片段") or "")
        resolved_params = (
            parse_param_declarations(params) if params is not None else params_from_template(template)
        )
        resolved_strict = True if strict_projection is None else strict_projection
    else:
        resolved_sql = str(sql if sql is not None else (entry or {}).get("查询SQL") or "")
        resolved_params = (
            parse_param_declarations(params)
            if params is not None
            else params_from_sql(resolved_sql)
        )
        resolved_strict = False if strict_projection is None else strict_projection

    resolved_fields = (
        split_field_list(return_fields)
        if return_fields is not None
        else split_field_list((entry or {}).get("返回字段"))
    )

    return _Candidate(
        sql=resolved_sql,
        params=tuple(resolved_params),
        return_fields=tuple(resolved_fields),
        template=template,
        template_ref=template_ref,
        strict_projection=resolved_strict,
        errors=errors,
    )


# ───────────────────────────────────────────────
# 契约版本仓库
# ───────────────────────────────────────────────

class SqlContractRegistry:
    """按接口保存契约版本，负责校验会话与原子发布。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: dict[str, list[ContractSnapshot]] = {}
        self._active: dict[str, ContractSnapshot] = {}
        self._sessions: dict[str, ValidationSession] = {}
        self._seed()

    # ── 查询 ────────────────────────────────────
    def reset(self, *, seed: bool = True) -> None:
        with self._lock:
            self._versions = {}
            self._active = {}
            self._sessions = {}
            if seed:
                self._seed()

    def _seed(self) -> None:
        """把示例数据里已经发布的接口固化成契约版本。"""
        seed_apis = [
            ("API-TRADE-001", "SQL-TRADE-001", "v2"),
            ("API-USER-001", "SQL-USER-001", "v3"),
        ]
        for api_code, template_ref, label in seed_apis:
            api = next((row for row in store.rows("api_service") if row.get("接口编号") == api_code), None)
            template = next(
                (row for row in store.rows("sql_template") if row.get("模板编号") == template_ref),
                None,
            )
            if api is None or template is None:
                continue
            sql_text = str(template.get("SQL片段") or "")
            params = params_from_template(template)
            fields = split_field_list(api.get("返回字段"))
            if validate_contract_fields(sql_text, params, fields, strict_projection=True):
                continue
            version_no = int(label[1:]) if label[:1] == "v" and label[1:].isdigit() else 1
            now = datetime.now().isoformat(timespec="seconds")
            snapshot = ContractSnapshot(
                api_code=api_code,
                version_no=version_no,
                version_label=label,
                sql=sql_text,
                params=params,
                return_fields=fields,
                template_id=int(template.get("id", 0)),
                template_ref=template_ref,
                template_version=str(template.get("模板版本", "")),
                checksum=compute_checksum(sql_text, params, fields),
                validated_at=now,
                published_at=now,
                change_note="示例数据初始化",
            )
            self._versions.setdefault(api_code, []).append(snapshot)
            self._active[api_code] = snapshot

    def active(self, api_code: str) -> ContractSnapshot | None:
        """读取接口当前生效版本；返回不可变快照。"""
        with self._lock:
            return self._active.get(api_code)

    def pin(self, api_code: str) -> ContractSnapshot | None:
        """为一次请求固定契约版本（与 active 同一份不可变对象）。"""
        return self.active(api_code)

    def get(self, api_code: str, version_no: int) -> ContractSnapshot | None:
        with self._lock:
            return next(
                (item for item in self._versions.get(api_code, []) if item.version_no == version_no),
                None,
            )

    def versions(self, api_code: str) -> list[ContractSnapshot]:
        with self._lock:
            return list(self._versions.get(api_code, []))

    def list_packages(self) -> list[dict[str, Any]]:
        with self._lock:
            packages = []
            for api_code in sorted(self._versions):
                active = self._active.get(api_code)
                if active is None:
                    continue
                packages.append({
                    "api_code": api_code,
                    "active_version": active.version_no,
                    "version_label": active.version_label,
                    "status": "已发布",
                    "template_ref": active.template_ref,
                    "params": [param.as_dict() for param in active.params],
                    "return_fields": list(active.return_fields),
                    "checksum": active.checksum,
                    "published_at": active.published_at,
                    "version_count": len(self._versions[api_code]),
                })
            return packages

    def session(self, token: str) -> ValidationSession | None:
        with self._lock:
            return self._sessions.get(token)

    # ── 校验会话 ────────────────────────────────
    def start_validation(
        self,
        api_code: str,
        *,
        entry: Mapping[str, Any] | None = None,
        template_id: int | None = None,
        sql: str | None = None,
        params: Any = None,
        return_fields: Any = None,
        strict_projection: bool | None = None,
        version_label: str | None = None,
        change_note: str = "",
    ) -> ValidationSession:
        candidate = build_candidate(
            entry,
            api_code=api_code,
            template_id=template_id,
            sql=sql,
            params=params,
            return_fields=return_fields,
            strict_projection=strict_projection,
        )
        token = uuid.uuid4().hex
        session = ValidationSession(
            token=token,
            api_code=api_code,
            status=SESSION_VALIDATING,
            sql=candidate.sql,
            params=candidate.params,
            return_fields=candidate.return_fields,
            template_id=int(candidate.template.get("id", 0)) if candidate.template else None,
            template_ref=candidate.template_ref,
            template_version=str(candidate.template.get("模板版本", "")) if candidate.template else "",
            fingerprint=fingerprint_template(candidate.template) if candidate.template else None,
            strict_projection=candidate.strict_projection,
            version_label=version_label,
            change_note=change_note,
            created_at=datetime.now().isoformat(timespec="seconds"),
            errors=list(candidate.errors),
        )
        with self._lock:
            self._sessions[token] = session
        return session

    def validate(self, token: str) -> ValidationSession:
        """执行完整校验；只有状态为“已校验”的会话才允许发布。"""
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise ContractError(f"校验会话 {token} 不存在")
            if session.status in (SESSION_ABORTED, SESSION_PUBLISHED):
                raise ContractError(f"校验会话当前状态为 {session.status}，不能继续校验")
            errors = list(session.errors)
            errors.extend(validate_contract_fields(
                session.sql,
                session.params,
                session.return_fields,
                strict_projection=session.strict_projection,
            ))
            # 去重但保留顺序
            deduped: list[str] = []
            for error in errors:
                if error not in deduped:
                    deduped.append(error)
            session.errors = deduped
            if deduped:
                session.status = SESSION_FAILED
            else:
                session.status = SESSION_VALIDATED
                session.validated_at = datetime.now().isoformat(timespec="seconds")
            return session

    def abort(self, token: str) -> ValidationSession:
        """中断校验：只丢弃候选版本，生效版本保持不变。"""
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise ContractError(f"校验会话 {token} 不存在")
            if session.status == SESSION_PUBLISHED:
                raise ContractError("已发布的契约版本不能中断")
            session.status = SESSION_ABORTED
            return session

    def publish(self, token: str) -> ContractSnapshot:
        """发布已校验契约；模板指纹变化或校验不完整时拒绝。"""
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise ContractError(f"校验会话 {token} 不存在")
            if session.status != SESSION_VALIDATED:
                raise ContractError(
                    f"契约尚未完整校验（当前状态：{session.status}），不能进入发布结果",
                    errors=list(session.errors),
                )
            if session.fingerprint is not None:
                template = store.find("sql_template", session.fingerprint.template_id)
                if template is None:
                    session.status = SESSION_STALE
                    session.errors = ["关联模板在校验期间被删除，本次校验结果失效"]
                    raise ContractError(session.errors[0], errors=session.errors)
                if fingerprint_template(template) != session.fingerprint:
                    session.status = SESSION_STALE
                    session.errors = [
                        "关联模板在校验期间被更正，本次校验的SQL/参数已过期，请重新校验后再发布"]
                    raise ContractError(session.errors[0], errors=session.errors)

            versions = self._versions.setdefault(session.api_code, [])
            version_no = max((item.version_no for item in versions), default=0) + 1
            label = session.version_label or f"v{version_no}"
            now = datetime.now().isoformat(timespec="seconds")
            snapshot = ContractSnapshot(
                api_code=session.api_code,
                version_no=version_no,
                version_label=label,
                sql=session.sql,
                params=session.params,
                return_fields=session.return_fields,
                template_id=session.template_id,
                template_ref=session.template_ref,
                template_version=session.template_version,
                checksum=compute_checksum(session.sql, session.params, session.return_fields),
                validated_at=session.validated_at or now,
                published_at=now,
                change_note=session.change_note,
            )
            # SQL、参数、返回字段同属一个不可变对象，这里一次赋值同时生效。
            versions.append(snapshot)
            self._active[session.api_code] = snapshot
            session.status = SESSION_PUBLISHED
            return snapshot

    def publish_candidate(
        self,
        api_code: str,
        *,
        entry: Mapping[str, Any] | None = None,
        template_id: int | None = None,
        sql: str | None = None,
        params: Any = None,
        return_fields: Any = None,
        version_label: str | None = None,
        change_note: str = "",
    ) -> ContractSnapshot:
        """一步完成校验+发布（发布动作内部使用）。"""
        session = self.start_validation(
            api_code,
            entry=entry,
            template_id=template_id,
            sql=sql,
            params=params,
            return_fields=return_fields,
            version_label=version_label,
            change_note=change_note,
        )
        validated = self.validate(session.token)
        if validated.status != SESSION_VALIDATED:
            raise ContractError(
                "发布前校验未通过：" + "；".join(validated.errors),
                errors=list(validated.errors),
            )
        return self.publish(session.token)


contract_registry = SqlContractRegistry()
