"""Small synchronous adapter for a host AI's tool registry. No model SDK needed."""
import json
from pathlib import Path
import subprocess
import sys

from agent_workflow import contract, validate_params


class WorkflowError(RuntimeError):
    """The caller can show this error or try a more specific search axis."""


def tool_definition():
    info = contract()
    return {"name": info["name"], "description": info["description"],
            "input_schema": info["input_schema"]}


def search(params: dict, *, cli_path=None, timeout=180, context=None) -> dict:
    """Dispatch a caller-made plan via stdin; return the source data and budget.

    Register this function as the implementation of ``tool_definition()``.
    The host AI decides terms and interprets returned sources in its own turn.
    Credentials stay in the host environment; workflow never reads AI keys.
    ``context`` is an optional trusted-host shared BOOTH budget, not a model arg.
    """
    try:
        validate_params(params)
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowError(str(exc)) from None
    request = {"action": "workflow", "params": params}
    if context is not None:
        request["context"] = context
    path = Path(cli_path) if cli_path is not None else Path(__file__).with_name("booth.py")
    prefix = [sys.executable, "-X", "utf8", str(path)] if path.suffix.lower() == ".py" else [str(path)]
    try:
        proc = subprocess.run(prefix + ["bot"], input=json.dumps(request, ensure_ascii=False),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        raise WorkflowError("BOOTH workflow 超时，未完成检索") from None
    except OSError as exc:
        raise WorkflowError(f"无法启动 BOOTH 工具: {type(exc).__name__}") from None
    try:
        envelope = json.loads(proc.stdout)
        if proc.returncode != 0 or not isinstance(envelope, dict):
            raise ValueError("bad envelope")
        if not envelope.get("ok"):
            raise WorkflowError(str(envelope.get("error") or "BOOTH workflow 未能完成"))
        data = envelope["data"]
        if not isinstance(data, dict) or data.get("ai_execution") != "caller":
            raise ValueError("unsupported caller contract")
    except (ValueError, KeyError, TypeError):
        raise WorkflowError("BOOTH CLI 未返回 caller 工作流契约，请使用 booth-cli 1.6.0+") from None
    return dict(data, request_budget=envelope.get("request_budget"))
