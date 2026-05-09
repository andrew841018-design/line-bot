"""Tests for finetune.modal_train pipeline (mock Modal SDK / Volume / GPU)。

不實際跑 modal run（沒 token + 燒 credit）。覆蓋：
  (a) local_entrypoint 上傳 jsonl + 觸發訓練 happy path
  (b) 訓練回 adapter → 寫 LOCAL_ADAPTER_PEFT/ → convert_adapter 轉 mlx
  (c) modal_cost.record_run 寫對 modal_runs.json + 超 80% 月度 cap 退出
  (d) eval gate 整合：過 / 沒過 兩情境
  (e) convert_adapter 的 peft → mlx key rename 邏輯
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── 確保 finetune/ 與 line_bot/ root 都在 sys.path ─────────────────────────

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
FT_DIR = LINE_BOT_ROOT / "finetune"
for p in (str(LINE_BOT_ROOT), str(FT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_distilled(tmp_path: Path) -> Path:
    """生 8 個 real + 2 個 mock pair 的 distilled.jsonl。"""
    p = tmp_path / "distilled.jsonl"
    rows = []
    for i in range(8):
        rows.append({
            "messages": [
                {"role": "user", "content": f"user msg {i}"},
                {"role": "assistant", "content": f"咪寶回應 {i} 內容夠長"},
            ],
            "metadata": {"distilled_at": 1778242477, "mock": False},
        })
    for i in range(2):
        rows.append({
            "messages": [
                {"role": "user", "content": f"mock {i}"},
                {"role": "assistant", "content": f"[MOCK] {i}"},
            ],
            "metadata": {"distilled_at": 1778242477, "mock": True},
        })
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


@pytest.fixture
def fresh_modal_train(monkeypatch, tmp_path):
    """Re-import modal_train 並 redirect 它的本機路徑到 tmp_path。"""
    # 把 finetune.modal_train 從 cache 移除以套上新環境
    for k in list(sys.modules):
        if k.startswith("finetune.modal_train") or k == "modal_train":
            del sys.modules[k]
    import finetune.modal_train as mt  # type: ignore

    # 改路徑
    monkeypatch.setattr(mt, "LOCAL_DATA", tmp_path / "distilled.jsonl")
    monkeypatch.setattr(mt, "LOCAL_ADAPTER_PEFT", tmp_path / "adapters" / "peft")
    monkeypatch.setattr(mt, "LOCAL_ADAPTER_MLX", tmp_path / "adapters" / "mlx")
    return mt


@pytest.fixture
def fresh_modal_cost(monkeypatch, tmp_path):
    for k in list(sys.modules):
        if k.startswith("finetune.modal_cost") or k == "modal_cost":
            del sys.modules[k]
    import finetune.modal_cost as mc  # type: ignore

    monkeypatch.setattr(mc, "DEFAULT_LOG", tmp_path / "modal_runs.json")
    return mc


# ─── (a) local_entrypoint：上傳 + 觸發訓練 happy path ────────────────────


def test_local_entrypoint_uploads_and_triggers_train(
    fresh_modal_train, tmp_distilled, tmp_path, monkeypatch
):
    """上傳 distilled.jsonl 後觸發 train_lora，下載 adapter 寫進本機 LOCAL_ADAPTER_PEFT。"""
    mt = fresh_modal_train
    # 把 fixture 的 distilled.jsonl 拷到 mt.LOCAL_DATA
    mt.LOCAL_DATA.parent.mkdir(parents=True, exist_ok=True)
    mt.LOCAL_DATA.write_bytes(tmp_distilled.read_bytes())

    fake_metrics = {
        "train_loss": 1.23,
        "eval_loss": 1.45,
        "num_train": 6,
        "num_eval": 2,
        "gpu_seconds": 7200.0,
        "estimated_cost_usd": 2.20,
        "base_model": mt.BASE_MODEL,
    }
    fake_files = {
        "adapter_config.json": b'{"r": 16}',
        "adapter_model.safetensors": b"fake-binary-content",
    }

    # mock Modal stub functions：他們的 .remote() 會被 local_entrypoint 呼叫
    mock_upload = MagicMock(return_value=len(tmp_distilled.read_bytes()))
    mock_train = MagicMock(return_value=fake_metrics)
    mock_download = MagicMock(return_value={"files": fake_files, "metrics": fake_metrics})

    # patch .remote attribute
    monkeypatch.setattr(mt.upload_data, "remote", mock_upload, raising=False)
    monkeypatch.setattr(mt.train_lora, "remote", mock_train, raising=False)
    monkeypatch.setattr(mt.download_adapter, "remote", mock_download, raising=False)

    # 跳過 cost 與 convert 與 eval（這些其他 test 各自驗）
    monkeypatch.setattr(mt, "_track_cost_locally", lambda m: {"month_total_usd": 2.2})
    monkeypatch.setattr(mt, "_convert_to_mlx", lambda p: p)
    monkeypatch.setattr(mt, "_run_eval_gate", lambda a, m: True)

    # 取出 local_entrypoint 的本體 function (modal 包了一層裝飾器，但有個 fn / func 屬性)
    entry = _unwrap_local_entrypoint(mt.main)

    # 8 real pair < 3000 → 必須 force=True
    entry(force=True, skip_eval=False)

    # ── assert：upload 被叫，傳的 bytes 跟本機檔內容相同 ───────────
    mock_upload.assert_called_once()
    sent_bytes = mock_upload.call_args[0][0]
    assert sent_bytes == mt.LOCAL_DATA.read_bytes()
    assert b"user msg 0" in sent_bytes

    # train 被叫
    mock_train.assert_called_once()
    # download 被叫
    mock_download.assert_called_once()

    # adapter 寫進本機
    assert (mt.LOCAL_ADAPTER_PEFT / "adapter_config.json").exists()
    assert (mt.LOCAL_ADAPTER_PEFT / "adapter_model.safetensors").exists()
    assert (mt.LOCAL_ADAPTER_PEFT / "adapter_model.safetensors").read_bytes() == b"fake-binary-content"


def test_local_entrypoint_aborts_if_data_missing(fresh_modal_train, monkeypatch):
    """LOCAL_DATA 不存在 → SystemExit(1)。"""
    mt = fresh_modal_train
    # 確保檔案不存在
    if mt.LOCAL_DATA.exists():
        mt.LOCAL_DATA.unlink()
    entry = _unwrap_local_entrypoint(mt.main)
    with pytest.raises(SystemExit) as exc:
        entry(force=True, skip_eval=True)
    assert exc.value.code == 1


def test_local_entrypoint_aborts_if_under_threshold_without_force(
    fresh_modal_train, tmp_distilled, monkeypatch
):
    """< 3000 real pairs 且未加 --force → SystemExit(2)。"""
    mt = fresh_modal_train
    mt.LOCAL_DATA.parent.mkdir(parents=True, exist_ok=True)
    mt.LOCAL_DATA.write_bytes(tmp_distilled.read_bytes())
    # mock remote 不該被叫
    monkeypatch.setattr(mt.upload_data, "remote", MagicMock(), raising=False)

    entry = _unwrap_local_entrypoint(mt.main)
    with pytest.raises(SystemExit) as exc:
        entry(force=False, skip_eval=True)
    assert exc.value.code == 2


# ─── (b) adapter 寫檔 + 轉 mlx ────────────────────────────────────────────


def test_write_adapter_locally_writes_files_with_subdirs(
    fresh_modal_train, tmp_path, monkeypatch
):
    """download_adapter payload 內 nested 路徑要正確還原 LOCAL_ADAPTER_PEFT/。"""
    mt = fresh_modal_train
    payload = {
        "files": {
            "adapter_config.json": b'{"r":16}',
            "checkpoint-1/adapter_model.safetensors": b"weights",
        },
        "metrics": {},
    }
    out = mt._write_adapter_locally(payload)
    assert out == mt.LOCAL_ADAPTER_PEFT
    assert (out / "adapter_config.json").exists()
    assert (out / "checkpoint-1" / "adapter_model.safetensors").read_bytes() == b"weights"


def test_convert_to_mlx_calls_convert_adapter(fresh_modal_train, tmp_path, monkeypatch):
    """_convert_to_mlx 呼叫 convert_adapter.convert(peft_path, MLX_PATH, base_model)。"""
    mt = fresh_modal_train
    fake_module = types.ModuleType("finetune.convert_adapter")
    fake_convert = MagicMock(return_value=mt.LOCAL_ADAPTER_MLX)
    fake_module.convert = fake_convert
    monkeypatch.setitem(sys.modules, "finetune.convert_adapter", fake_module)

    peft = mt.LOCAL_ADAPTER_PEFT
    peft.mkdir(parents=True, exist_ok=True)
    out = mt._convert_to_mlx(peft)
    assert out == mt.LOCAL_ADAPTER_MLX
    fake_convert.assert_called_once()
    # 第一 / 第二 positional：peft_path、out_dir
    call_args = fake_convert.call_args
    assert call_args.args[0] == peft
    assert call_args.args[1] == mt.LOCAL_ADAPTER_MLX
    # base_model kwarg 對齊
    assert call_args.kwargs.get("base_model") == mt.BASE_MODEL


# ─── (c) modal_cost 紀錄 + cap ───────────────────────────────────────────


def test_record_run_writes_log_under_cap(fresh_modal_cost):
    """正常情況：寫 modal_runs.json，回月度累計 dict。"""
    mc = fresh_modal_cost
    summary = mc.record_run(
        gpu_seconds=3600,
        cost_usd=1.10,
        metrics={"train_loss": 0.5},
        enforce_cap=True,
    )
    assert summary["this_run_usd"] == 1.10
    assert summary["month_total_usd"] >= 1.10
    log = json.loads(mc.DEFAULT_LOG.read_text(encoding="utf-8"))
    assert len(log["runs"]) == 1
    assert log["runs"][0]["gpu_seconds"] == 3600
    assert log["runs"][0]["cost_usd"] == 1.10
    assert log["runs"][0]["metrics"]["train_loss"] == 0.5


def test_record_run_enforces_monthly_cap(fresh_modal_cost):
    """超過 80% 月度 cap → SystemExit。"""
    mc = fresh_modal_cost
    # 先 seed 一筆 $19 的 run（cap=$25, 80%=$20）
    mc.record_run(gpu_seconds=60000, cost_usd=19.0, metrics={}, enforce_cap=False)
    # 再加 $2 → 累 $21 >= $20，應退出
    with pytest.raises(SystemExit):
        mc.record_run(gpu_seconds=7200, cost_usd=2.0, metrics={}, enforce_cap=True)


def test_record_run_cap_warn_can_be_disabled(fresh_modal_cost):
    """enforce_cap=False 時即便超 80% 也不退出。"""
    mc = fresh_modal_cost
    mc.record_run(gpu_seconds=60000, cost_usd=24.0, metrics={}, enforce_cap=False)
    # 不該爆
    summary = mc.record_run(gpu_seconds=7200, cost_usd=2.0, metrics={}, enforce_cap=False)
    assert summary["used_pct"] > 80


# ─── (d) eval gate 整合：過 / 沒過 ────────────────────────────────────────


def test_run_eval_gate_passes_when_modules_missing(fresh_modal_train, tmp_path, monkeypatch):
    """eval_harness / acceptance_gate 不存在 → 視為 stub 通過。"""
    mt = fresh_modal_train
    # 確保 stub modules 不存在
    monkeypatch.setitem(sys.modules, "eval_harness", None)
    monkeypatch.setitem(sys.modules, "acceptance_gate", None)
    # patch builtins.__import__ 讓 eval_harness/acceptance_gate import 失敗
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("eval_harness", "acceptance_gate"):
            raise ImportError(f"no {name}")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        ok = mt._run_eval_gate(tmp_path / "adapter", {})
    assert ok is True


def test_run_eval_gate_fails_when_acceptance_rejects(fresh_modal_train, tmp_path, monkeypatch):
    """eval_harness 跑 OK 但 acceptance_gate.check 回 False → False。"""
    mt = fresh_modal_train
    fake_eval = types.ModuleType("eval_harness")
    fake_eval.run = MagicMock(return_value={"perplexity": 99.0})
    fake_gate = types.ModuleType("acceptance_gate")
    fake_gate.check = MagicMock(return_value=False)
    monkeypatch.setitem(sys.modules, "eval_harness", fake_eval)
    monkeypatch.setitem(sys.modules, "acceptance_gate", fake_gate)

    ok = mt._run_eval_gate(tmp_path / "adapter", {"train_loss": 1.0})
    assert ok is False
    fake_eval.run.assert_called_once()
    fake_gate.check.assert_called_once()


def test_run_eval_gate_passes_when_acceptance_ok(fresh_modal_train, tmp_path, monkeypatch):
    """eval + gate 都 OK → True；嘗試呼 activate_adapter。"""
    mt = fresh_modal_train
    fake_eval = types.ModuleType("eval_harness")
    fake_eval.run = MagicMock(return_value={"perplexity": 5.0})
    fake_gate = types.ModuleType("acceptance_gate")
    fake_gate.check = MagicMock(return_value=True)
    fake_activate = types.ModuleType("finetune.activate_adapter")
    fake_activate.activate = MagicMock()
    monkeypatch.setitem(sys.modules, "eval_harness", fake_eval)
    monkeypatch.setitem(sys.modules, "acceptance_gate", fake_gate)
    monkeypatch.setitem(sys.modules, "finetune.activate_adapter", fake_activate)

    ok = mt._run_eval_gate(tmp_path / "adapter", {"train_loss": 1.0})
    assert ok is True


# ─── (e) convert_adapter peft → mlx key rename ───────────────────────────


def test_peft_key_to_mlx_renames_lora_AB():
    from finetune import convert_adapter as ca

    src = "base_model.model.model.layers.0.self_attn.q_proj.lora_A.default.weight"
    expected = "model.layers.0.self_attn.q_proj.lora_a"
    assert ca._peft_key_to_mlx(src) == expected

    src_b = "base_model.model.model.layers.31.self_attn.v_proj.lora_B.default.weight"
    expected_b = "model.layers.31.self_attn.v_proj.lora_b"
    assert ca._peft_key_to_mlx(src_b) == expected_b


def test_peft_key_to_mlx_returns_none_for_unknown():
    from finetune import convert_adapter as ca

    # base model 自身權重不該被當 lora
    assert ca._peft_key_to_mlx("model.embed_tokens.weight") is None
    # bias key 也忽略
    assert ca._peft_key_to_mlx("base_model.model.foo.bias") is None


def test_convert_writes_mlx_format(tmp_path, monkeypatch):
    """完整轉換流程：peft → mlx 寫出 adapter_config + adapters.safetensors。"""
    from finetune import convert_adapter as ca

    # 先建一份假的 peft adapter
    peft = tmp_path / "peft"
    peft.mkdir()
    peft_cfg = {
        "r": 16,
        "lora_alpha": 32,
        "lora_dropout": 0.05,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "base_model_name_or_path": "Qwen/Qwen2.5-3B-Instruct",
    }
    (peft / "adapter_config.json").write_text(json.dumps(peft_cfg), encoding="utf-8")

    # 用 torch + safetensors 寫一個假 weights file
    try:
        import torch
        from safetensors.torch import save_file
    except Exception:
        pytest.skip("torch / safetensors not available")
    fake_weights = {
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.default.weight": torch.zeros((16, 4)),
        "base_model.model.model.layers.0.self_attn.q_proj.lora_B.default.weight": torch.zeros((4, 16)),
        # 一個 unknown key 應被 skip
        "base_model.model.junk.weight": torch.zeros((1,)),
    }
    save_file(fake_weights, str(peft / "adapter_model.safetensors"))

    out = tmp_path / "mlx"
    result = ca.convert(peft, out, base_model="mlx-community/Qwen2.5-3B-Instruct-4bit")
    assert result == out
    cfg = json.loads((out / "adapter_config.json").read_text(encoding="utf-8"))
    assert cfg["fine_tune_type"] == "lora"
    assert cfg["lora_parameters"]["rank"] == 16
    assert cfg["lora_parameters"]["scale"] == pytest.approx(2.0)
    assert "q_proj" in cfg["lora_parameters"]["keys"]
    assert (out / "adapters.safetensors").exists()


# ─── helpers ─────────────────────────────────────────────────────────────


def _unwrap_local_entrypoint(maybe_decorated):
    """Modal 的 @stub.local_entrypoint() 會包一層；找出底下 callable。

    實作上 modal 包成 LocalEntrypoint object，有 raw_f / func 屬性。失敗就直接 call。
    """
    for attr in ("raw_f", "func", "fn", "_fn"):
        f = getattr(maybe_decorated, attr, None)
        if callable(f):
            return f
    if callable(maybe_decorated):
        return maybe_decorated
    raise RuntimeError(f"can't unwrap local_entrypoint: {maybe_decorated}")
