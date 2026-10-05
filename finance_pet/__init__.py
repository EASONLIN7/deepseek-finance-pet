"""赛博财务管家 —— 挂在 Codex 原生桌宠上的 DeepSeek 余额 / 消耗面板。

核心思路：Codex 的桌宠是一个独立的 Electron 覆盖窗口（avatar overlay），
它把窗口位置写在 ``$CODEX_HOME/.codex-global-state.json`` 里。本程序

1. 读取该文件，实时算出桌宠在屏幕上的精确矩形；
2. 用一个全局鼠标钩子监听落在该矩形内的左键点击（等价于 pet.onClick）；
3. 点击时在桌宠上方弹出一个无边框透明气泡，显示 DeepSeek 余额与今日消耗。

所有网络与记账逻辑都在本地进程内完成，不需要改动 Codex 本体。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
