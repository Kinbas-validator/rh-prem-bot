"""Render a local preview from the same formatters used by the actual bot."""

from html import escape
from pathlib import Path

from rh_prem_bot.app import demo_snapshot
from rh_prem_bot.messages import alert_text, keyboard, ranking_panel
from rh_prem_bot.models import Market, Trade
from rh_prem_bot.store import Store


def render_buttons(markup):
    return (
        '<div class="buttons">'
        + "".join(
            '<div class="button-row">'
            + "".join('<button type="button">' + escape(b["text"]) + "</button>" for b in row)
            + "</div>"
            for row in markup["inline_keyboard"]
        )
        + "</div>"
    )


def main():
    store = Store(":memory:")
    stamp = demo_snapshot().end_ms
    entries = [
        ("NVDA", 35000, 0.55),
        ("ANTHROPIC", 12000, 0.51),
        ("OPENAI", 3900, 0.76),
        ("TSLA", 3300, 0.8),
        ("MSFT", 2800, 0.68),
        ("AAPL", 2100, 0.5),
        ("AMD", 1900, 0.73),
        ("SPCX", 1600, 0.63),
        ("META", 1400, 0.41),
        ("SHEIN", 800, 0.64),
    ]
    markets = []
    for i, (symbol, rate, share) in enumerate(entries):
        markets.append(Market(i, symbol, "perp"))
        for j, (usd, fee) in enumerate([(rate * 5, 350), (rate * 5 / share - rate * 5, 0)]):
            store.add_trades(
                [
                    Trade.parse(
                        {
                            "trade_id": j + 1,
                            "market_id": i,
                            "timestamp": stamp - 1000,
                            "usd_amount": f"{usd:.6f}",
                            "ask_account_id": 10000 + i,
                            "bid_account_id": 20000 + i,
                            "is_maker_ask": True,
                            "taker_fee": fee,
                        }
                    )
                ]
            )
    table, markup = ranking_panel(
        store, markets, 5, stamp, {0, 3, 4, 5, 6, 8, 9}, {m.id: stamp - 3600000 for m in markets}
    )
    message = alert_text(demo_snapshot()).replace("\n", "<br>")
    page = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RWA Premium Monitor — предпросмотр</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0e1621;color:#edf3f7;
font:18px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
main{max-width:690px;margin:24px auto;padding:0 18px}header{color:#91a4b4;
font-size:13px;letter-spacing:.04em;margin-bottom:25px}article{margin-bottom:25px}
.bubble{background:#182b3a;border-radius:20px 20px 7px 7px;padding:15px 18px 11px;
box-shadow:0 1px 0 #070e16}a{color:#70c5ef;text-decoration:none}b{font-weight:650}
.time{font-size:13px;color:#7f94a5;text-align:right;margin-top:3px}
.buttons{display:grid;gap:4px;margin-top:4px}.button-row{display:flex;gap:4px}
button{min-width:0;flex:1;border:0;border-radius:7px;background:#1d3142;color:#edf3f7;
padding:13px 7px;font:500 16px/1.25 Arial,sans-serif;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;cursor:pointer}button:hover{background:#2a4459}
pre{font:16px/1.5 "DejaVu Sans Mono",monospace;color:#8bb8d9;background:#080e17;
padding:16px 14px;border-left:4px solid #5483a6;border-radius:5px;
margin:12px 0 3px;overflow:auto}.user{text-align:right;margin:12px 0 20px}
.user span{display:inline-block;background:#2b5278;padding:10px 20px;border-radius:15px}
footer{font-size:12px;color:#7f94a5;margin:28px 2px}
</style><main><header>RWA PREMIUM MONITOR · ЛОКАЛЬНЫЙ МАКЕТ · СИНТЕТИЧЕСКИЕ ДАННЫЕ</header>
"""
    page += '<article><div class="bubble">' + message + '<div class="time">20:21</div></div>'
    page += render_buttons(keyboard(1)) + '</article><div class="user"><span>/start ✓✓</span></div>'
    # Preserve preformatted line breaks instead of replacing them with BR inside PRE.
    before, rest = table.split("<pre>", 1)
    pre, after = rest.split("</pre>", 1)
    page += '<article><div class="bubble">' + before.replace("\n", "<br>")
    page += "<pre>" + pre + "</pre>" + after.replace("\n", "<br>")
    page += '<div class="time">20:21</div></div>' + render_buttons(markup) + "</article>"
    page += "<footer>Структура формируется кодом действующего Telegram-бота. "
    page += "Цвета, размеры и переносы в Telegram зависят от темы и устройства. "
    page += (
        "Для проверки интерактивности запустите бота; кнопки этого макета "
        "статические.</footer></main></html>"
    )
    Path("docs/preview.html").write_text(page)
    store.close()
    print("docs/preview.html")


if __name__ == "__main__":
    main()
