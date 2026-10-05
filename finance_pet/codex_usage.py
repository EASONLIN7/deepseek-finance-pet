"""从 Codex 的 rollout 日志中增量抽取 token 用量。

Codex 会把每个会话写成 ``$CODEX_HOME/sessions/<年>/<月>/<日>/rollout-*.jsonl``，
其中既有会话元信息（模型、provider），也有每次调用的
``token_usage_record``。本模块只做"读文件 + 入账本"，不联网。

采用 offset 增量扫描：已读过的字节不再重复解析，重复启动也不会重复计费。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .ledger import Ledger, UsageEvent
from .pricing import cost_for_usage
from typing import Mapping


def iter_rollout_files(sessions_dir: Path) -> Iterator[Path]:
    """按修改时间从旧到新遍历 rollout 文件。"""
    if not sessions_dir.is_dir():
        return
    files = [p for p in sessions_dir.rglob("rollout-*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    yield from files


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """从 offset 读取到文件末尾，返回 (完整行, 新 offset)。"""
    size = path.stat().st_size
    if offset > size:  # 文件被截断/重写
        offset = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return [], offset
    # 只消费到最后一个换行，避免读到半行
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], offset
    consumed = chunk[: last_nl + 1]
    lines = consumed.decode("utf-8", errors="replace").splitlines()
    return lines, offset + last_nl + 1


def scan_sessions(
    *,
    ledger: Ledger,
    sessions_dir: Path,
    default_model: str,
    fx_usd_cny: float = 7.1,
    holidays: tuple[str, ...] = (),
    prices: Mapping[str, Mapping[str, float]] | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """增量扫描全部 rollout，把新的用量写进账本。

    返回 ``{"files": n, "new_events": n, "skipped": n}``。
    """
    files_seen = 0
    new_events = 0
    skipped = 0

    for path in iter_rollout_files(sessions_dir):
        files_seen += 1
        stat = path.stat()
        state = None if reset else ledger.get_scan_state(path)
        offset = int(state["offset"]) if state else 0
        model = (state["model"] if state else None) or default_model
        provider = (state["provider"] if state else None) or "deepseek"

        # mtime/size 都没变就跳过，避免全量读盘
        if state and int(state["size"]) == stat.st_size and float(state["mtime"]) == stat.st_mtime:
            skipped += 1
            continue

        lines, new_offset = _read_new_lines(path, offset)
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rtype = record.get("type")
            payload = record.get("payload") or {}

            if rtype == "session_meta":
                model = payload.get("model") or model
                provider = payload.get("model_provider") or provider
                continue

            if rtype != "token_usage_record":
                continue

            usage = payload.get("usage") or payload.get("last_token_usage") or {}
            if not usage:
                continue
            ts = _parse_ts(record.get("timestamp"))
            cost = cost_for_usage(
                model,
                usage,
                at=ts,
                fx_usd_cny=fx_usd_cny,
                prices=prices,
                holidays=holidays,
            )
            event = UsageEvent(
                ts=ts,
                model=model,
                provider=provider,
                thread_id=payload.get("thread_id"),
                turn_id=payload.get("turn_id"),
                session_id=payload.get("session_id"),
                response_id=payload.get("response_id"),
                input_tokens=int(usage.get("input_tokens") or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cost_usd=float(cost["amount_usd"]),
                cost_amount=float(cost["amount"]),
                currency="CNY",
                tier=str(cost["tier"]),
                assumed_model=bool(cost["assumed_model"]),
            )
            if ledger.record(event):
                new_events += 1

        ledger.set_scan_state(
            path,
            offset=new_offset,
            mtime=stat.st_mtime,
            size=stat.st_size,
            model=model,
            provider=provider,
        )

    return {"files": files_seen, "new_events": new_events, "skipped": skipped}
