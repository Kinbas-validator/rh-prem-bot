from __future__ import annotations

import html
from datetime import UTC, datetime

from .analytics import Snapshot


def amount(value: float) -> str:
    return f"{value:,.0f}"


def alert_text(s: Snapshot) -> str:
    window = f"{s.window_seconds // 60}м" if s.window_seconds % 60 == 0 else f"{s.window_seconds}с"
    text = (
        f'⚠️ <a href="https://robinhoodchain.lighter.xyz/">{html.escape(s.symbol)}</a>'
        f" — Всплеск premium объёма\n"
        f"<b>{amount(s.premium_per_minute)}$/мин</b> за {window} "
        f"(среднее {amount(s.baseline_per_minute)}$/мин, x{s.ratio:.1f})\n"
        f"объем {amount(s.total_per_minute)}$/мин, премиум <b>{s.share:.0%}</b>\n"
        f"prem→stand {amount(s.flows.get('premium->standard', 0))}$ | "
        f"stand→prem {amount(s.flows.get('standard->premium', 0))}$ | "
        f"prem→prem {amount(s.flows.get('premium->premium', 0))}$\n"
        f"премиум-аккаунтов: {len(s.premium_accounts)}"
    )
    extra = sum(v for k, v in s.flows.items() if "plus" in k or "unknown" in k)
    if extra:
        text += f"\nс участием Plus/неизвестных: {amount(extra)}$"
    return text


def detail_text(s: Snapshot) -> str:
    when = datetime.fromtimestamp(s.end_ms / 1000, UTC).strftime("%d.%m.%Y %H:%M:%S UTC")
    direction = "maker → taker" if s.flow_direction == "maker_taker" else "продавец → покупатель"
    lines = [
        f"📈 <b>{html.escape(s.symbol)} — подробнее</b>",
        when,
        f"Окно: {s.window_seconds}с; сделок: {s.trade_count}",
        f"Объём: {amount(s.total_usd)}$; premium: {amount(s.premium_usd)}$ ({s.share:.1%})",
        f"Среднее premium: {amount(s.baseline_per_minute)}$/мин "
        f"за {s.baseline_seconds / 60:.1f} мин; x{s.ratio:.2f}",
        f"Направления: {direction}",
    ]
    for flow, value in sorted(s.flows.items()):
        lines.append(f"{html.escape(flow.replace('->', ' → '))}: {amount(value)}$")
    lines += [
        f"Неизвестная ставка хотя бы одной стороны: {amount(s.unknown_usd)}$",
        f"Premium account index: {len(s.premium_accounts)}; топ по участию:",
    ]
    for account, volume in s.account_volumes[:15]:
        lines.append(f"• <code>{account}</code>: {amount(volume)}$")
    lines.append(
        "Участие двух premium учитывается в общем объёме один раз. "
        "Account index — субаккаунт, не уникальный человек. "
        "Тип определён по ставке комиссии в момент сделки."
    )
    if not s.ready:
        lines.append("⏳ Идёт накопление базового периода; сигналы ещё отключены.")
    return "\n".join(lines)


def keyboard(alert_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "📈 Подробнее", "callback_data": f"detail:{alert_id}"}]]}


def compact(value: float) -> str:
    for scale, suffix in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if value >= scale:
            number = f"{value / scale:.0f}" if value / scale >= 10 else f"{value / scale:.1f}"
            return number.removesuffix(".0") + suffix
    return f"{value:.0f}"


def ranking_panel(
    store, markets, minutes: int, end_ms: int, muted: set[int], observed_since: dict[int, int]
) -> tuple[str, dict]:
    sums = store.ranking(end_ms - minutes * 60000, end_ms)
    rows = sorted(
        [(m, *sums.get(m.id, (0, 0))) for m in markets], key=lambda row: (-row[1], row[0].symbol)
    )[:10]
    when = datetime.fromtimestamp(end_ms / 1000, UTC).strftime("%H:%M:%S")
    lines = [f" # {'Token':<12} {'Prem':>6} {'%':>4} {'Vol':>6}"]
    for i, (market, premium, total) in enumerate(rows, 1):
        share = premium / total if total else 0
        lines.append(
            f"{i:>2} {market.symbol:<12} {compact(premium / minutes):>6} "
            f"{share:>4.0%} {compact(total / minutes):>6}"
        )
    text = (
        f"📈 Премиум за <b>{minutes}м</b>, $/мин · {when} UTC\n"
        f"<pre>{html.escape(chr(10).join(lines))}</pre>"
    )
    if any(
        mid not in observed_since or end_ms - observed_since[mid] < minutes * 60000
        for mid in (r[0].id for r in rows)
    ):
        text += f"\n⏳ Накапливается история за {minutes}м."
    buttons = [
        [
            {"text": ("• " if n == minutes else "") + f"{n}м", "callback_data": f"rank:{n}"}
            for n in (1, 5, 15, 60)
        ]
        + [{"text": "⟳", "callback_data": f"rank:{minutes}"}]
    ]
    tickers = [
        {
            "text": ("🔊 " if m.id not in muted else "") + m.symbol,
            "callback_data": f"toggle:{minutes}:{m.id}",
        }
        for m, _, _ in rows
    ]
    buttons.extend([tickers[i : i + 5] for i in range(0, len(tickers), 5)])
    buttons.append([{"text": "❮ Вернуться в меню", "callback_data": "menu"}])
    return text, {"inline_keyboard": buttons}


def menu_panel() -> tuple[str, dict]:
    return (
        "📈 <b>RWA Premium Monitor</b>\nАкции и pre-IPO · Lighter / Robinhood Chain",
        {
            "inline_keyboard": [
                [{"text": "📈 Премиум объём", "callback_data": "rank:5"}],
                [
                    {"text": "⚙️ Настройки", "callback_data": "settings"},
                    {"text": "📡 Статус", "callback_data": "status"},
                ],
                [{"text": "📖 Справка", "callback_data": "help"}],
            ]
        },
    )
