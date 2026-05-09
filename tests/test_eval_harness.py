"""Tests for finetune eval_harness + acceptance_gate + train_lora chain."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# bootstrap env (對齊 conftest.py)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

# 確保 finetune/ 在 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "finetune"))


# ── Metric 1: 違規率 ────────────────────────────────────────────────────────


def test_metric_violation_clean():
    from finetune import eval_harness

    v, _r = eval_harness.metric_violation("我覺得這檔股票不錯。", "AAPL 怎樣")
    assert v is False


def test_metric_violation_echo_opener():
    from finetune import eval_harness

    v, r = eval_harness.metric_violation("咪寶看到您剛剛說的內容。", "你看到了嗎")
    assert v is True
    assert "echo opener" in r


def test_metric_violation_empty_phrase():
    from finetune import eval_harness

    v, r = eval_harness.metric_violation(
        "我覺得這個現象，真的讓人很心疼。", "心情不好"
    )
    assert v is True
    assert "empty phrase" in r


# ── Metric 2: 中文比率 ──────────────────────────────────────────────────────


def test_metric_chinese_ratio_pure_traditional():
    from finetune import eval_harness

    m = eval_harness.metric_chinese_ratio("這個我覺得不錯。")
    assert m["passes"] is True
    assert m["cn_ratio"] == 1.0
    assert m["simplified_ratio"] == 0.0


def test_metric_chinese_ratio_too_much_english():
    from finetune import eval_harness

    m = eval_harness.metric_chinese_ratio("apple banana cat dog elephant fox 你")
    assert m["passes"] is False
    assert m["cn_ratio"] < 0.95


def test_metric_chinese_ratio_simplified_present():
    from finetune import eval_harness

    # 全簡體字 → simplified_ratio 應該很高
    m = eval_harness.metric_chinese_ratio("这个汉语东时间发现这个为问题")
    assert m["simplified_ratio"] > 0.05
    assert m["passes"] is False


# ── Metric 3: 規則 0 first-sentence-take ─────────────────────────────────


def test_metric_rule0_pass_with_opinion_marker():
    from finetune import eval_harness

    m = eval_harness.metric_rule0_first_sentence("我覺得這檔不太行。應該避開。")
    assert m["passes"] is True


def test_metric_rule0_pass_with_number():
    from finetune import eval_harness

    m = eval_harness.metric_rule0_first_sentence("AAPL 今天收 175 美元。")
    assert m["passes"] is True


def test_metric_rule0_fail_blacklist_opener():
    from finetune import eval_harness

    m = eval_harness.metric_rule0_first_sentence("這張圖片展示了一個風景。")
    assert m["passes"] is False
    assert "blacklist" in m["reason"]


def test_metric_rule0_fail_no_marker():
    from finetune import eval_harness

    m = eval_harness.metric_rule0_first_sentence("天氣很好風景也漂亮。沒什麼好說。")
    assert m["passes"] is False


def test_metric_rule0_fail_empty():
    from finetune import eval_harness

    m = eval_harness.metric_rule0_first_sentence("")
    assert m["passes"] is False


# ── Judge JSON parse ──────────────────────────────────────────────────────


def test_parse_judge_output_plain_json():
    from finetune import eval_harness

    o = eval_harness._parse_judge_output('{"score": 8, "reason": "good"}')
    assert o == {"score": 8, "reason": "good"}


def test_parse_judge_output_with_codefence():
    from finetune import eval_harness

    o = eval_harness._parse_judge_output('```json\n{"score": 9, "reason": "ok"}\n```')
    assert o is not None
    assert o["score"] == 9


def test_parse_judge_output_clamped():
    from finetune import eval_harness

    # > 10 應該被夾到 10
    o = eval_harness._parse_judge_output('{"score": 99}')
    assert o["score"] == 10
    # < 0 應該被夾到 0
    o = eval_harness._parse_judge_output('{"score": -3}')
    assert o["score"] == 0


def test_parse_judge_output_invalid():
    from finetune import eval_harness

    assert eval_harness._parse_judge_output("totally not json") is None
    assert eval_harness._parse_judge_output("") is None


# ── data split ────────────────────────────────────────────────────────────


def _write_jsonl(p: Path, pairs: list[dict]) -> None:
    with p.open("w", encoding="utf-8") as f:
        for x in pairs:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")


def test_load_test_set_dedupe_and_split(tmp_path):
    from finetune import eval_harness

    # 12 unique pairs，20% → 2 條 hold-out
    pairs = []
    for i in range(12):
        pairs.append({
            "messages": [
                {"role": "user", "content": f"問題 {i}"},
                {"role": "assistant", "content": f"回答 {i}"},
            ],
            "metadata": {"mock": False},
        })
    distilled = tmp_path / "distilled.jsonl"
    sft = tmp_path / "sft.jsonl"
    _write_jsonl(distilled, pairs[:8])
    _write_jsonl(sft, pairs[8:])

    test = eval_harness.load_test_set(distilled=distilled, sft=sft)
    assert len(test) == max(1, int(12 * 0.2))
    # 同 seed 跑兩次應該完全一樣 (deterministic)
    test2 = eval_harness.load_test_set(distilled=distilled, sft=sft)
    assert [t["user"] for t in test] == [t["user"] for t in test2]


def test_load_test_set_skips_mock(tmp_path):
    from finetune import eval_harness

    pairs = [
        {
            "messages": [
                {"role": "user", "content": "real"},
                {"role": "assistant", "content": "real_reply"},
            ],
            "metadata": {"mock": False},
        },
        {
            "messages": [
                {"role": "user", "content": "mock"},
                {"role": "assistant", "content": "mock_reply"},
            ],
            "metadata": {"mock": True},
        },
    ]
    distilled = tmp_path / "distilled.jsonl"
    sft = tmp_path / "sft.jsonl"
    sft.write_text("", encoding="utf-8")
    _write_jsonl(distilled, pairs)
    # All loaded (after dedup + mock filter) → 1 pair → 20% × 1 = 1 (max(1,...))
    test = eval_harness.load_test_set(distilled=distilled, sft=sft)
    # 應該只有 real 那條被收進來，不會出現 mock
    all_texts = [p["user"] for p in test]
    assert "mock" not in all_texts


# ── evaluate() 主流程（mock LLM） ─────────────────────────────────────────


def test_evaluate_with_mock_llm(monkeypatch):
    from finetune import eval_harness

    test_set = [
        {"user": "今天天氣？", "gold": "我覺得今天好像不錯。"},
        {"user": "AAPL 走勢？", "gold": "我看是會盤整。"},
    ]

    def gen(u: str) -> str:
        if "天氣" in u:
            return "我覺得今天天氣不錯。"
        return "我看是 175 美元附近盤整。"

    def judge(u, g, p):
        return {"score": 8, "reason": "ok"}

    r = eval_harness.evaluate(
        test_set, generate_fn=gen, judge_fn=judge, label="test"
    )
    m = r["metrics"]
    assert m["n_total"] == 2
    assert m["violation_rate"] == 0.0
    assert m["chinese_pass_rate"] == 1.0
    assert m["rule0_pass_rate"] == 1.0
    assert m["judge_avg_score"] == 8.0
    assert m["judge_n"] == 2


def test_evaluate_skip_judge(monkeypatch):
    from finetune import eval_harness

    test_set = [{"user": "X", "gold": "Y"}]

    def gen(u):
        return "我覺得這個還可以。"

    r = eval_harness.evaluate(
        test_set, generate_fn=gen, skip_judge=True, label="x"
    )
    assert r["metrics"]["judge_avg_score"] is None
    assert r["metrics"]["judge_n"] == 0


def test_evaluate_judge_returns_none_counts_as_skipped(monkeypatch):
    from finetune import eval_harness

    test_set = [
        {"user": "問題 1", "gold": "回 1"},
        {"user": "問題 2", "gold": "回 2"},
    ]

    def gen(u):
        return "我覺得 OK。"

    # 第 1 條 None（quota 爆），第 2 條給分
    calls = {"n": 0}

    def judge(u, g, p):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return {"score": 9, "reason": "good"}

    r = eval_harness.evaluate(test_set, generate_fn=gen, judge_fn=judge, label="x")
    assert r["metrics"]["judge_skipped"] == 1
    assert r["metrics"]["judge_n"] == 1
    assert r["metrics"]["judge_avg_score"] == 9.0


# ── acceptance_gate ──────────────────────────────────────────────────────


def test_acceptance_gate_all_pass():
    from finetune import acceptance_gate

    base = {
        "violation_rate": 0.4,
        "chinese_pass_rate": 0.90,
        "rule0_pass_rate": 0.50,
        "judge_avg_score": 6.5,
    }
    adp = {
        "violation_rate": 0.10,  # 0.4 * 0.5 = 0.2 → 0.10 OK
        "chinese_pass_rate": 0.97,
        "rule0_pass_rate": 0.85,
        "judge_avg_score": 7.5,  # delta 1.0 ≥ 0.5 AND 7.5 ≥ 7.0 OK
    }
    g = acceptance_gate.evaluate_gates(base, adp)
    assert all(v["pass"] for v in g.values())
    assert acceptance_gate.all_pass(g) is True


def test_acceptance_gate_g1_fail():
    from finetune import acceptance_gate

    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 6.0}
    adp = {"violation_rate": 0.30, "chinese_pass_rate": 1.0,
           "rule0_pass_rate": 1.0, "judge_avg_score": 7.5}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G1_violation"]["pass"] is False
    assert acceptance_gate.all_pass(g) is False


def test_acceptance_gate_g2_fail():
    from finetune import acceptance_gate

    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 6.0}
    adp = {"violation_rate": 0.10, "chinese_pass_rate": 0.80,
           "rule0_pass_rate": 1.0, "judge_avg_score": 7.5}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G2_chinese"]["pass"] is False
    assert acceptance_gate.all_pass(g) is False


def test_acceptance_gate_g3_fail():
    from finetune import acceptance_gate

    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 6.0}
    adp = {"violation_rate": 0.10, "chinese_pass_rate": 1.0,
           "rule0_pass_rate": 0.50, "judge_avg_score": 7.5}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G3_rule0"]["pass"] is False


def test_acceptance_gate_g4_delta_fail():
    from finetune import acceptance_gate

    # 絕對值 ≥ 7 但 delta < 0.5
    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 7.5}
    adp = {"violation_rate": 0.10, "chinese_pass_rate": 1.0,
           "rule0_pass_rate": 1.0, "judge_avg_score": 7.6}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G4_judge"]["pass"] is False


def test_acceptance_gate_g4_abs_fail():
    from finetune import acceptance_gate

    # delta 大但絕對值 < 7
    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 4.0}
    adp = {"violation_rate": 0.10, "chinese_pass_rate": 1.0,
           "rule0_pass_rate": 1.0, "judge_avg_score": 6.5}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G4_judge"]["pass"] is False


def test_acceptance_gate_judge_none_fails():
    from finetune import acceptance_gate

    base = {"violation_rate": 0.4, "chinese_pass_rate": 1.0,
            "rule0_pass_rate": 1.0, "judge_avg_score": 6.0}
    adp = {"violation_rate": 0.10, "chinese_pass_rate": 1.0,
           "rule0_pass_rate": 1.0, "judge_avg_score": None}
    g = acceptance_gate.evaluate_gates(base, adp)
    assert g["G4_judge"]["pass"] is False


# ── update local_llm_config ──────────────────────────────────────────────


def test_update_active_adapter_existing_var(tmp_path):
    from finetune import acceptance_gate

    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text(textwrap.dedent("""
        LOCAL_LLM_MODEL = "x"
        LOCAL_LLM_FALLBACKS = []
        ACTIVE_ADAPTER = None
    """).strip() + "\n", encoding="utf-8")
    acceptance_gate.update_active_adapter("/tmp/myadapter", config_path=cfg)
    text = cfg.read_text()
    assert 'ACTIVE_ADAPTER = "/tmp/myadapter"' in text
    # 不應該重複 append
    assert text.count("ACTIVE_ADAPTER") == 1


def test_update_active_adapter_missing_var(tmp_path):
    from finetune import acceptance_gate

    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text(textwrap.dedent("""
        LOCAL_LLM_MODEL = "x"
        LOCAL_LLM_FALLBACKS = []
    """).strip() + "\n", encoding="utf-8")
    acceptance_gate.update_active_adapter("/tmp/foo", config_path=cfg)
    text = cfg.read_text()
    assert 'ACTIVE_ADAPTER = "/tmp/foo"' in text


def test_clear_active_adapter(tmp_path):
    from finetune import acceptance_gate

    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text('ACTIVE_ADAPTER = "/tmp/foo"\n', encoding="utf-8")
    acceptance_gate.clear_active_adapter(config_path=cfg)
    assert "ACTIVE_ADAPTER = None" in cfg.read_text()


# ── acceptance_gate.main() pass / fail 整合 ───────────────────────────


def _write_metrics(p: Path, m: dict) -> None:
    p.write_text(json.dumps({"metrics": m, "samples": []}), encoding="utf-8")


def test_acceptance_gate_main_pass_writes_config(tmp_path):
    from finetune import acceptance_gate

    base_p = tmp_path / "baseline.json"
    adp_p = tmp_path / "adapter.json"
    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")

    _write_metrics(base_p, {
        "violation_rate": 0.4, "chinese_pass_rate": 0.9,
        "rule0_pass_rate": 0.5, "judge_avg_score": 6.5,
    })
    _write_metrics(adp_p, {
        "violation_rate": 0.1, "chinese_pass_rate": 0.97,
        "rule0_pass_rate": 0.85, "judge_avg_score": 7.5,
    })

    with patch.object(acceptance_gate, "CONFIG_PATH", cfg):
        rc = acceptance_gate.main([
            "--baseline", str(base_p),
            "--adapter-result", str(adp_p),
            "--adapter-path", "/tmp/adapter",
        ])
    assert rc == 0
    assert 'ACTIVE_ADAPTER = "/tmp/adapter"' in cfg.read_text()


def test_acceptance_gate_main_fail_does_not_write_config(tmp_path):
    from finetune import acceptance_gate

    base_p = tmp_path / "baseline.json"
    adp_p = tmp_path / "adapter.json"
    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")

    _write_metrics(base_p, {
        "violation_rate": 0.4, "chinese_pass_rate": 1.0,
        "rule0_pass_rate": 1.0, "judge_avg_score": 6.0,
    })
    # adapter 違規率沒減半 → G1 fail
    _write_metrics(adp_p, {
        "violation_rate": 0.30, "chinese_pass_rate": 1.0,
        "rule0_pass_rate": 1.0, "judge_avg_score": 7.5,
    })

    with patch.object(acceptance_gate, "CONFIG_PATH", cfg):
        rc = acceptance_gate.main([
            "--baseline", str(base_p),
            "--adapter-result", str(adp_p),
            "--adapter-path", "/tmp/adapter",
        ])
    assert rc != 0
    # config 不該被改
    assert "ACTIVE_ADAPTER = None" in cfg.read_text()
    assert "/tmp/adapter" not in cfg.read_text()


def test_acceptance_gate_main_dry_run(tmp_path):
    from finetune import acceptance_gate

    base_p = tmp_path / "baseline.json"
    adp_p = tmp_path / "adapter.json"
    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")

    _write_metrics(base_p, {
        "violation_rate": 0.4, "chinese_pass_rate": 0.9,
        "rule0_pass_rate": 0.5, "judge_avg_score": 6.5,
    })
    _write_metrics(adp_p, {
        "violation_rate": 0.1, "chinese_pass_rate": 0.97,
        "rule0_pass_rate": 0.85, "judge_avg_score": 7.5,
    })

    with patch.object(acceptance_gate, "CONFIG_PATH", cfg):
        rc = acceptance_gate.main([
            "--baseline", str(base_p),
            "--adapter-result", str(adp_p),
            "--adapter-path", "/tmp/adapter",
            "--dry-run",
        ])
    assert rc == 0
    # dry-run 不該改 config
    assert "ACTIVE_ADAPTER = None" in cfg.read_text()


# ── train_lora chain 整合 ────────────────────────────────────────────────


def test_train_lora_chain_eval_pass_updates_config(tmp_path, monkeypatch):
    """模擬 _run_eval_and_gate：subprocess.call 三次（baseline / adapter / gate）。
    全 0 → return 0；config 由 acceptance_gate.main() 真寫入。
    """
    from finetune import train_lora as tl

    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()

    eval_dir = tmp_path / "eval_results"
    eval_dir.mkdir()
    base_out = eval_dir / "baseline.json"
    adp_out = eval_dir / "adapter.json"
    _write_metrics(base_out, {
        "violation_rate": 0.4, "chinese_pass_rate": 0.9,
        "rule0_pass_rate": 0.5, "judge_avg_score": 6.5,
    })
    _write_metrics(adp_out, {
        "violation_rate": 0.1, "chinese_pass_rate": 0.97,
        "rule0_pass_rate": 0.85, "judge_avg_score": 7.5,
    })

    monkeypatch.setattr(tl, "EVAL_RESULTS_DIR", eval_dir)

    from finetune import acceptance_gate

    monkeypatch.setattr(acceptance_gate, "CONFIG_PATH", cfg)

    def fake_call(cmd, *args, **kwargs):
        # baseline / adapter eval 假裝成功（output 我們已預先寫好）
        path = cmd[1] if len(cmd) > 1 else ""
        if "eval_harness.py" in path:
            return 0
        # acceptance_gate 真跑：用 main() in-process
        if "acceptance_gate.py" in path:
            argv = cmd[2:]
            return acceptance_gate.main(argv)
        return 0

    monkeypatch.setattr(tl.subprocess, "call", fake_call)

    rc = tl._run_eval_and_gate(adapter_dir)
    assert rc == 0
    assert f'ACTIVE_ADAPTER = "{adapter_dir}"' in cfg.read_text()


def test_train_lora_chain_eval_fail_does_not_update_config(tmp_path, monkeypatch):
    from finetune import train_lora as tl

    cfg = tmp_path / "local_llm_config.py"
    cfg.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()

    eval_dir = tmp_path / "eval_results"
    eval_dir.mkdir()
    base_out = eval_dir / "baseline.json"
    adp_out = eval_dir / "adapter.json"
    # adapter 違規率比 baseline 還高 → G1 fail
    _write_metrics(base_out, {
        "violation_rate": 0.2, "chinese_pass_rate": 1.0,
        "rule0_pass_rate": 1.0, "judge_avg_score": 8.0,
    })
    _write_metrics(adp_out, {
        "violation_rate": 0.30, "chinese_pass_rate": 1.0,
        "rule0_pass_rate": 1.0, "judge_avg_score": 7.0,
    })

    monkeypatch.setattr(tl, "EVAL_RESULTS_DIR", eval_dir)

    from finetune import acceptance_gate

    monkeypatch.setattr(acceptance_gate, "CONFIG_PATH", cfg)

    def fake_call(cmd, *args, **kwargs):
        path = cmd[1] if len(cmd) > 1 else ""
        if "eval_harness.py" in path:
            return 0
        if "acceptance_gate.py" in path:
            return acceptance_gate.main(cmd[2:])
        return 0

    monkeypatch.setattr(tl.subprocess, "call", fake_call)

    rc = tl._run_eval_and_gate(adapter_dir)
    assert rc != 0
    # config 不該被改
    assert "ACTIVE_ADAPTER = None" in cfg.read_text()
    assert str(adapter_dir) not in cfg.read_text()
