---
name: deepseek-finance-pet
description: Put a DeepSeek balance and spend dashboard on the Codex desktop pet. Use when the user wants to see their DeepSeek balance, asks how much they have spent, wants a low-balance or runaway-spend warning, wants a desktop pet click panel, or wants to track API spend locally. Installs a companion process that reads the pet position from Codex state, listens for clicks on the pet, and pops a themed bubble with live balance. Windows only.
metadata:
  short-description: DeepSeek balance bubble on the Codex pet
---

# DeepSeek Finance Pet

Turn the Codex desktop pet into a spend dashboard: click it, get a bubble with your
**live DeepSeek balance** and **today's spend**.

**Target setup: DeepSeek on Codex (`deepseek-codex`).** This skill is built for
Codex running against the DeepSeek API — the balance endpoint, the price table and
the rollout-log accounting all assume `api.deepseek.com`. It also reads your key
from Codex's own `[model_providers.deepseek]` block, so a DeepSeek-backed Codex
needs no extra configuration.

Codex's pet ships as declarative sprite data (`pet.json` + an 8x11 atlas) with no
script hooks, so it cannot be extended from the inside. This skill adds a companion
process instead — it locates the pet, listens for clicks on it, and draws a layered
window above it. Nothing about Codex or the pet is modified.

## Use this skill when

- The user asks what their DeepSeek balance is, or wants it always one click away.
- The user asks how much they have spent today, this week, or in the last half hour.
- The user wants a warning before they run out of credit, or when spend spikes.
- The user wants a click panel on the Codex desktop pet.

## Do not use this skill when

- The user is on macOS or Linux. The click hook and the overlay are Windows-only
  (`WH_MOUSE_LL` plus a layered window); see "Porting" below.
- The user only wants a one-off balance number. `GET https://api.deepseek.com/user/balance`
  with their key is enough; no install needed.
- The user wants to modify the pet's artwork. That is a different job — the pet is
  an 8x11 sprite atlas, not a scriptable UI.

## Install

Requires Python 3.9+ and Pillow. The Codex-bundled runtime already has Pillow.

```powershell
.\run.ps1 -AutoDetect   # locate the pet by frame-differencing its idle animation
.\run.ps1 -Install      # set up autostart and launch it now
.\run.ps1 -Status       # should say "运行中：1 个进程"
```

## How it works

| Piece | What it does |
|---|---|
| `finance_pet/pet_locator.py` | Reads the pet rect from `.codex-global-state.json`, or enumerates the overlay window |
| `finance_pet/screen_probe.py` | Auto-calibrates: the pet keeps animating while the UI is still, so frame differencing isolates its true pixel box |
| `finance_pet/click_hook.py` | `WH_MOUSE_LL` hook: a left click landing on the pet rect acts as `pet.onClick`; also tracks drag for follow |
| `finance_pet/balance.py` | Calls `GET /user/balance`, caches the last success, and degrades to friendly text on failure |
| `finance_pet/codex_usage.py` | Incrementally parses `~/.codex/sessions/**/rollout-*.jsonl` for per-call token usage |
| `finance_pet/pricing.py` | DeepSeek price table with peak / off-peak tiers |
| `finance_pet/alerts.py` | Low-balance and 30-minute burst rules with cooldowns |
| `finance_pet/win32_window.py` | Per-pixel-alpha layered window with click hit-testing |

## Configuration

Everything lives in `%CODEX_HOME%\finance-pet\config.json` (usually
`C:\Users\<you>\.codex\finance-pet\config.json`). Every key is optional.

```json
{
  "auto_hide_seconds": 3,
  "low_balance_threshold": 5,
  "burst_window_minutes": 30,
  "burst_threshold": 2,
  "follow_drag": true
}
```

See `config.example.json` for the full set, and the README for what each does.

## Hard rules

1. **Never print the API key.** It is read at runtime from `DEEPSEEK_API_KEY`, the
   config file, or Codex's own `config.toml`; the status line prints only the source.
2. **Never write the key to disk.** Do not copy it into config.json, a log, or a test.
3. **Keep the spend numbers honest.** "Today's spend" counts *local Codex usage*
   reconstructed from rollout logs. Usage from the web UI or other tools is not in
   that number — the balance endpoint is the source of truth. Say so when reporting.
4. **Do not claim the pet was modified.** The pet is untouched; this is a companion.
5. **A click only counts if it lands on the pet's window.** Codex leaves the pet
   rect in `.codex-global-state.json` after it exits, so a coordinate-only check
   turns that screen area into a permanent invisible hot zone. Always keep
   `require_pet_window` on unless the user's environment genuinely needs it off.

## Verify

```powershell
.\run.ps1 -Test          # 31 unit tests
.\run.ps1 -Where         # current pet rect and where it came from
.\run.ps1 -Console -Show # foreground run with logs, pops the bubble once
```

## Troubleshooting

If a click does nothing: `run.ps1 -Status` to confirm it is running, then
`-AutoDetect` to re-calibrate, then `-Console` with `$env:FINANCE_PET_DEBUG=1` to see
whether the click hook fires. If the rect is right but no bubble appears, the layered
window failed — `win32_window.last_error` will say why.

## Porting

The rendering, ledger, pricing and balance layers are platform-neutral. Only
`click_hook.py` and `win32_window.py` are Windows-specific; swap them for a
platform equivalent and the rest carries over.
