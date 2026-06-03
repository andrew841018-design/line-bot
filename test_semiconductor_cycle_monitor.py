import sys
import types

import daily_briefing_discord as dbd


def _cycle_metrics_fixture():
    return [
        {
            "label": "TSMC 月營收 YoY",
            "status": "✅ 未觸發",
            "detail": "2026-04 營收 NT$410.7B，YoY +17.5%，MoM -1.1%；YoY 連 1 次放緩",
            "source": "TSMC IR parquet",
        },
        {
            "label": "NVDA / AVGO / AMD / MU 指引（proxy）",
            "status": "✅ 未觸發",
            "detail": "NVDA 營收+85.2% / EPS+214.5%；弱化 0/4 家",
            "source": "yfinance revenueGrowth/earningsGrowth proxy",
        },
        {
            "label": "ASML / AMAT / LRCX 訂單（proxy）",
            "status": "✅ 未觸發",
            "detail": "ASML 營收+13.2% / EPS+19.2%；弱化 0/3 家",
            "source": "yfinance revenueGrowth/earningsGrowth proxy",
        },
        {
            "label": "HBM / DRAM / NAND（MU proxy）",
            "status": "✅ 未觸發",
            "detail": "MU 營收+196.3% / EPS+756.0%；DRAM/NAND 免費歷史價源缺",
            "source": "yfinance MU proxy + local DRAM source note",
        },
        {
            "label": "Hyperscaler capex",
            "status": "✅ 未觸發",
            "detail": "2026-03 四大雲廠合計 capex $91.2B，平均 YoY +78.8%，QoQ +12.4%",
            "source": "stockanalysis quarterly cash-flow parquet",
        },
        {
            "label": "SOXX EPS revision breadth（proxy）",
            "status": "✅ 未觸發",
            "detail": "SOXX top10 EPS 成長為正 9/10 (90%)；最弱：INTC -12.0%",
            "source": "SOXX holdings + yfinance earningsGrowth proxy",
        },
    ]


def test_semiconductor_cycle_monitor_covers_cycle_exit_signals():
    msg = dbd.semiconductor_cycle_monitor(metrics=_cycle_metrics_fixture())

    assert "半導體週期監控" in msg
    assert "SOXX 週期單" in msg
    assert "不因創高出清" in msg
    assert "2 項觸發先減碼" in msg
    assert "3-4 項大幅降倉" in msg
    assert "5 項以上結束週期單" in msg

    for token in (
        "TSMC 月營收 YoY",
        "NVDA / AVGO / AMD / MU",
        "ASML / AMAT / LRCX",
        "HBM / DRAM / NAND",
        "Hyperscaler capex",
        "SOXX EPS revision breadth",
    ):
        assert token in msg

    assert "2026-04 營收 NT$410.7B" in msg
    assert "營收+85.2% / EPS+214.5%" in msg
    assert "capex $91.2B" in msg
    assert "proxy" in msg
    assert "MA20" not in msg
    assert "20 日線" not in msg


def test_main_includes_semiconductor_cycle_monitor(monkeypatch):
    sent: list[str] = []
    original_monitor = dbd.semiconductor_cycle_monitor

    fake_pkg = types.ModuleType("integration")
    fake_pkg.__path__ = []
    fake_module = types.ModuleType("integration.briefing_section")
    fake_module.soxx_briefing_section = lambda: ""
    fake_pkg.briefing_section = fake_module
    monkeypatch.setitem(sys.modules, "integration", fake_pkg)
    monkeypatch.setitem(sys.modules, "integration.briefing_section", fake_module)

    monkeypatch.setattr(dbd, "send_dm", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(dbd, "daily_todos", lambda: "📌 **每日待辦**")
    monkeypatch.setattr(dbd, "interview_prep_today", lambda: "")
    monkeypatch.setattr(dbd, "daily_tech_note", lambda: "")
    monkeypatch.setattr(dbd, "upcoming_birthdays", lambda: "")
    monkeypatch.setattr(dbd, "crawler_status", lambda: "")
    monkeypatch.setattr(dbd, "line_bot_status", lambda: "")
    monkeypatch.setattr(dbd, "git_status", lambda: "")
    monkeypatch.setattr(dbd, "system_status", lambda: "")
    monkeypatch.setattr(dbd, "line_bot_suggestions", lambda: "")
    monkeypatch.setattr(dbd, "_try_append_today_quote", lambda: None)
    monkeypatch.setattr(
        dbd,
        "semiconductor_cycle_monitor",
        lambda: original_monitor(metrics=_cycle_metrics_fixture()),
    )
    monkeypatch.setattr(
        dbd,
        "sox_sentiment",
        lambda: (_ for _ in ()).throw(AssertionError("sox_sentiment should not run")),
    )
    monkeypatch.setattr(dbd, "jim_cramer_daily", lambda: "")

    dbd.main()

    assert sent
    text = "\n".join(sent)
    assert "半導體週期監控" in text
    assert "TSMC 月營收 YoY" in text
    assert "2026-04 營收 NT$410.7B" in text
    assert "費城半導體指數 (^SOX)" not in text
