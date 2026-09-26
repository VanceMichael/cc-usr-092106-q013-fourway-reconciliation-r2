"""加载 contracts/ 下的 JSON Schema，并做标准库可支持的最小结构校验。

仅支持本仓库契约实际用到的 JSON Schema 关键字：
type / required / properties / items / enum / minimum / minLength /
minItems / additionalProperties / $ref(#/$defs)。
"""

import json
from pathlib import Path

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contracts"

_CONTENT_SCHEMA = {
    "invoice_pack": "invoice_pack.schema.json",
    "ledger_snippet": "ledger_snippet.schema.json",
    "stock_count": "stock_count.schema.json",
    "payment_summary": "payment_summary.schema.json",
    "explanation": "explanation.schema.json",
}


class ContractError(ValueError):
    """材料结构不符合契约。"""


def load_schema(name: str) -> dict:
    return json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))


def validate(instance, schema: dict, path: str = "$", root: dict | None = None) -> None:
    """按受限关键字集合递归校验，错误信息指向具体字段路径。"""
    if root is None:
        root = schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ContractError(f"不支持的引用 {ref}")
        target = root["$defs"][ref.split("/")[-1]]
        validate(instance, target, path, root)
        return

    expected = schema.get("type")
    if expected is not None and not _matches_type(instance, expected):
        raise ContractError(f"{path} 类型应为 {expected}，实际为 {type(instance).__name__}")

    if "enum" in schema and instance not in schema["enum"]:
        raise ContractError(f"{path} 的值 {instance!r} 不在允许范围 {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ContractError(f"{path} 长度不足 {schema['minLength']}")
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ContractError(f"{path} 小于最小值 {schema['minimum']}")
    elif isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ContractError(f"{path} 至少需要 {schema['minItems']} 项")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                validate(item, item_schema, f"{path}[{i}]", root)
    elif isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                raise ContractError(f"{path} 缺少必填字段 {key}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                validate(value, properties[key], f"{path}.{key}", root)
            elif schema.get("additionalProperties") is False:
                raise ContractError(f"{path} 出现未声明字段 {key}")


def _matches_type(instance, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    if isinstance(expected, list):
        return any(_matches_type(instance, t) for t in expected)
    return True


def validate_envelope(envelope: dict) -> None:
    validate(envelope, load_schema("envelope.schema.json"))
    content_schema = _CONTENT_SCHEMA.get(envelope["material_type"])
    if content_schema is not None:
        validate(envelope["content"], load_schema(content_schema))
