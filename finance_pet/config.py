"""配置解析：默认值 -> 配置文件 -> 环境变量，并安全地取出 API Key。

API Key 的优先级（先命中先用）：

1. 环境变量 ``DEEPSEEK_API_KEY``
2. 配置文件里的 ``api_key`` / ``api_key_file``
3. Codex 自己的 ``$CODEX_HOME/config.toml`` 中
   ``[model_providers.<provider>].experimental_bearer_token``

第 3 条意味着：只要你已经用 DeepSeek 跑 Codex，就**不需要再单独配置 Key**。
Key 只在进程内存里使用，既不落盘也不会发给任何前端。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


def codex_home() -> Path:
    """返回 Codex 的 home 目录（默认 ``~/.codex``）。"""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


@dataclass
class ApiKey:
    """API Key 及其来源描述（``value`` 永远不要写进日志）。"""

    value: str | None = None
    source: str = "未配置"

    @property
    def ok(self) -> bool:
        return bool(self.value)


@dataclass
class Config:
    # --- 接口 ---
    api_base: str = "https://api.deepseek.com"
    api_key: ApiKey = field(default_factory=ApiKey)
    provider: str = "deepseek"
    timeout: float = 8.0
    retries: int = 1

    # --- 计价 ---
    currency: str = "CNY"          # 展示货币：CNY 或 USD
    fx_usd_cny: float = 7.1        # USD -> CNY 折算率（官方按 USD 计价）
    default_model: str = "deepseek-flash"
    holidays: tuple[str, ...] = ()  # 额外视为错峰的日期 "YYYY-MM-DD"
    prices: dict[str, dict[str, float]] | None = None  # 覆盖官方价格表

    # --- 外观 ---
    theme: str = "auto"            # auto | dark | light
    auto_hide_seconds: float = 3.0    # 点击弹出的气泡，3 秒后自动关闭
    alert_hide_seconds: float = 8.0   # 主动提醒的气泡留久一点，便于看清
    width: int = 320
    offset: int = 8                # 气泡与桌宠之间的间距(px)

    # --- 主动提醒 ---
    alerts_enabled: bool = True
    alerts_poll_seconds: float = 90.0        # 后台自检周期
    low_balance_threshold: float = 5.0       # 余额低于该值提醒
    low_balance_cooldown_minutes: float = 60.0
    burst_window_minutes: float = 30.0       # "过去的半小时"
    burst_threshold: float = 2.0             # 窗口内消耗超过该值提醒
    burst_cooldown_minutes: float = 30.0

    # --- 桌宠接入 ---
    global_state_path: Path = field(
        default_factory=lambda: codex_home() / ".codex-global-state.json"
    )
    mascot_rect: tuple[int, int, int, int] | None = None  # 手动覆盖 (x,y,w,h)
    mascot_offset: tuple[int, int, int, int] | None = None  # 叠加在自动定位结果上的 (dx,dy,dw,dh)
    click_padding: int = 10
    follow_drag: bool = True     # 拖动桌宠时气泡实时跟随
    hotkey: str | None = "ctrl+alt+b"   # 备用快捷键；None 表示关闭

    # --- 存储 ---
    data_dir: Path = field(default_factory=lambda: codex_home() / "finance-pet")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finance.db"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "balance_cache.json"

    @property
    def sessions_dir(self) -> Path:
        return codex_home() / "sessions"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _read_codex_key(config_toml: Path, provider: str) -> str | None:
    """从 Codex 的 config.toml 里读取 provider 的 bearer token。"""
    if tomllib is None or not config_toml.is_file():
        return None
    try:
        with config_toml.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return None
    providers = data.get("model_providers") or {}
    entry = providers.get(provider) or {}
    for key in ("experimental_bearer_token", "api_key", "bearer_token"):
        token = entry.get(key)
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _read_codex_theme(config_toml: Path) -> str | None:
    """从 Codex 的 config.toml 读取当前外观主题（dark/light）。"""
    if tomllib is None or not config_toml.is_file():
        return None
    try:
        with config_toml.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return None
    theme = ((data.get("desktop") or {}).get("appearanceTheme"))
    return theme if theme in {"dark", "light"} else None


def _resolve_key(raw: dict[str, Any], provider: str) -> ApiKey:
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return ApiKey(env_key, "环境变量 DEEPSEEK_API_KEY")

    inline = str(raw.get("api_key") or "").strip()
    if inline:
        return ApiKey(inline, "配置文件 api_key")

    key_file = raw.get("api_key_file")
    if key_file:
        path = Path(key_file).expanduser()
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                return ApiKey(text, f"密钥文件 {path.name}")

    codex_key = _read_codex_key(codex_home() / "config.toml", provider)
    if codex_key:
        return ApiKey(codex_key, f"Codex config.toml (model_providers.{provider})")

    return ApiKey(None, "未找到密钥")


def load_config(config_file: Path | None = None) -> Config:
    """载入配置。配置文件不存在时使用默认值。"""
    raw: dict[str, Any] = {}
    candidates = [config_file] if config_file else []
    env_data_dir = os.environ.get("FINANCE_PET_DATA_DIR", "").strip()
    candidates.append(codex_home() / "finance-pet" / "config.json")
    if env_data_dir:
        candidates.append(Path(env_data_dir).expanduser() / "config.json")
    candidates.append(Path.cwd() / "config.json")
    loaded_from: Path | None = None
    for path in candidates:
        if path and path.is_file():
            try:
                # utf-8-sig 兼容记事本等编辑器写入的 BOM
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                loaded_from = path
            except Exception:
                raw = {}
            break

    provider = str(raw.get("provider") or "deepseek")
    cfg = Config(
        provider=provider,
        api_base=str(raw.get("api_base") or "https://api.deepseek.com").rstrip("/"),
        timeout=float(raw.get("timeout") or 8.0),
        retries=int(raw.get("retries") or 1),
        currency=str(raw.get("currency") or "CNY").upper(),
        fx_usd_cny=float(raw.get("fx_usd_cny") or 7.1),
        default_model=str(raw.get("default_model") or "deepseek-flash"),
        holidays=tuple(str(d) for d in (raw.get("holidays") or ())),
        theme=str(raw.get("theme") or "auto").lower(),
        # 缺省 3 秒；显式写 0 表示"一直停留不自动关闭"
        auto_hide_seconds=3.0 if raw.get("auto_hide_seconds") is None else float(raw["auto_hide_seconds"]),
        alert_hide_seconds=float(raw.get("alert_hide_seconds") or 8.0),
        width=int(raw.get("width") or 320),
        offset=int(raw.get("offset") or 8),
        click_padding=int(raw.get("click_padding") or 10),
        follow_drag=_as_bool(raw.get("follow_drag"), True),
        alerts_enabled=_as_bool(raw.get("alerts_enabled"), True),
        alerts_poll_seconds=float(raw.get("alerts_poll_seconds") or 90.0),
        low_balance_threshold=float(raw.get("low_balance_threshold") or 5.0),
        low_balance_cooldown_minutes=float(raw.get("low_balance_cooldown_minutes") or 60.0),
        burst_window_minutes=float(raw.get("burst_window_minutes") or 30.0),
        burst_threshold=float(raw.get("burst_threshold") or 2.0),
        burst_cooldown_minutes=float(raw.get("burst_cooldown_minutes") or 30.0),
    )

    if raw.get("mascot_rect"):
        rect = tuple(int(v) for v in raw["mascot_rect"])
        if len(rect) == 4:
            cfg.mascot_rect = rect  # type: ignore[assignment]

    if raw.get("mascot_offset"):
        offset = tuple(int(v) for v in raw["mascot_offset"])
        if len(offset) == 4:
            cfg.mascot_offset = offset  # type: ignore[assignment]

    pricing_override = raw.get("pricing")
    if isinstance(pricing_override, dict):
        table: dict[str, dict[str, float]] = {}
        for model, spec in pricing_override.items():
            if model.startswith("_") or not isinstance(spec, dict):
                continue  # 跳过 _comment 之类的说明字段
            try:
                table[model] = {
                    "cache_hit": float(spec["cache_hit"]),
                    "cache_miss": float(spec["cache_miss"]),
                    "output": float(spec["output"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        if table:
            cfg.prices = table

    if "hotkey" in raw:
        hk = raw.get("hotkey")
        cfg.hotkey = None if hk in (None, "", False) else str(hk)

    if raw.get("data_dir"):
        cfg.data_dir = Path(str(raw["data_dir"])).expanduser()
    if raw.get("global_state_path"):
        cfg.global_state_path = Path(str(raw["global_state_path"])).expanduser()

    cfg.api_key = _resolve_key(raw, provider)

    # 环境变量覆盖（便于多环境 / 便携运行 / 自动化测试）
    if os.environ.get("FINANCE_PET_DATA_DIR"):
        cfg.data_dir = Path(os.environ["FINANCE_PET_DATA_DIR"]).expanduser()
    if os.environ.get("FINANCE_PET_GLOBAL_STATE"):
        cfg.global_state_path = Path(os.environ["FINANCE_PET_GLOBAL_STATE"]).expanduser()
    env_hotkey = os.environ.get("FINANCE_PET_HOTKEY")
    if env_hotkey is not None:
        cleaned = env_hotkey.strip().lower()
        cfg.hotkey = None if cleaned in {"", "none", "off", "0"} else cleaned

    if cfg.theme == "auto":
        cfg.theme = _read_codex_theme(codex_home() / "config.toml") or "dark"

    cfg.loaded_from = loaded_from  # type: ignore[attr-defined]
    return cfg
