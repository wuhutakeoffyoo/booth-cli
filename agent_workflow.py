"""Caller-led workflow contract and source export. Standard library; no AI calls."""
import hashlib
import json

import search_evidence


def contract():
    """An OpenAI-style tool definition can use input_schema unchanged."""
    return {
        "schema_version": 1,
        "name": "booth_workflow",
        "description": "Search BOOTH with caller-provided terms and return unverified public product sources. The caller AI plans, judges relevance and checks compatibility; this tool never calls a model.",
        "ai_execution": "caller",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "keyword": {"type": "array", "minItems": 1, "maxItems": 6,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                            "description": "Exact BOOTH search axes; not split or rewritten. Omit to search the original query."},
                "require_term": {"type": "array", "maxItems": 8,
                                 "items": {"type": "string", "minLength": 1, "maxLength": 100},
                                 "description": "Locate source excerpts for requirements; does not confirm compatibility."},
                "sort": {"type": "string", "enum": ["new", "popularity", "liked", "price_asc", "price_desc"], "default": "popularity"},
                "adult": {"type": "string", "enum": ["exclude", "include", "only"], "default": "include"},
                "no_vrc": {"type": "boolean", "default": False},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 6, "default": 6},
                "desc_len": {"type": "integer", "anyOf": [{"minimum": 1, "maximum": 12000}, {"const": -1}], "default": 3000},
                "no_cache": {"type": "boolean", "default": False},
            },
        },
        "transport": {"command": ["booth", "bot"], "stdin": {"action": "workflow", "params": {"query": "为桔梗找衣装", "keyword": ["桔梗 衣装"], "require_term": ["桔梗"]}}},
        "result_contract": {
            "schema_version": 1, "ai_execution": "caller", "ai_calls": 0,
            "source_trust": "untrusted_data", "relevance_status": "unknown",
            "compatibility_status": "unknown",
            "items": "Public listing metadata, description, description_truncated, source_hash, source_excerpt and detail_status; fetch item with desc_len=-1 if truncated.",
            "warnings": "search_incomplete and/or detail_unavailable; empty incomplete results do not prove absence.",
        },
    }


def _strings(values, label, count, max_chars):
    if not isinstance(values, list) or len(values) > count:
        raise ValueError(f"{label} 必须是列表，最多 {count} 项")
    result, seen = [], set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_chars:
            raise ValueError(f"{label} 每项须为 1-{max_chars} 字符的非空字符串")
        value = value.strip()
        key = search_evidence.normalized(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def validate_params(params):
    """Reject ill-typed JSON before argparse can coerce it to a different intent."""
    properties = contract()["input_schema"]["properties"]
    properties = dict(properties, schema={"type": "boolean"}, json={"type": "boolean"})
    for name, value in params.items():
        if name not in properties:
            raise ValueError(f"workflow 不支持参数 {name}")
        kind = properties[name]["type"]
        types = {"string": str, "array": list, "integer": int, "boolean": bool}
        if type(value) is not types[kind]:
            raise ValueError(f"workflow 参数 {name} 须为 {kind}")
        if kind == "array":
            spec = properties[name]
            if len(value) < spec.get("minItems", 0):
                raise ValueError(f"workflow 参数 {name} 不能为空列表")
            _strings(value, name, spec["maxItems"], spec["items"]["maxLength"])


def search_plan(query_parts, keywords, limit, page, desc_len, required_terms):
    query = " ".join(query_parts or []).strip()
    if not query or len(query) > 1000:
        raise ValueError("query 须为 1-1000 字符的原始需求")
    if not 1 <= limit <= 6 or page < 1:
        raise ValueError("limit 须为 1-6，page 须大于等于 1")
    if desc_len != -1 and not 1 <= desc_len <= 12000:
        raise ValueError("desc_len 须为 1-12000 或 -1")
    terms = _strings(keywords, "keyword", 6, 200) if keywords is not None else [query]
    if not terms:
        raise ValueError("keyword 不能为空列表")
    _strings(required_terms or [], "require_term", 8, 100)
    return query, terms


def result(query, terms, candidates, *, candidate_count, search_info,
           required_terms, desc_len, sort_note):
    items = []
    for candidate in candidates:
        item = {key: value for key, value in candidate.items() if not key.startswith("_")}
        fields = search_evidence.source_fields(candidate)
        description = fields["description"]
        item.update(
            description=description if desc_len == -1 else description[:desc_len],
            description_truncated=desc_len != -1 and len(description) > desc_len,
            source_excerpt=search_evidence.excerpt(description, required_terms or terms),
            source_hash=hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            relevance_status="unknown", compatibility_status="unknown",
        )
        items.append(item)
    warnings = []
    if search_info.get("_search_failures"):
        warnings.append("search_incomplete")
    if any(item.get("detail_status") != "available" for item in items):
        warnings.append("detail_unavailable")
    return {
        "schema_version": 1, "query": query, "keywords": terms,
        "require_terms": required_terms, "ai_execution": "caller", "ai_calls": 0,
        "source_trust": "untrusted_data", "assessment": "caller_required",
        "candidate_count": candidate_count, "count": len(items),
        "first_axis_total": search_info.get("total"),
        "has_next": bool(search_info.get("has_next")),
        "sort_note": sort_note or None, "warnings": warnings, "items": items,
    }
