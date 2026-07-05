import builtins

import local_llm


def test_ensure_loaded_skips_broken_hf_cache_symlink(monkeypatch, tmp_path):
    missing_target = tmp_path / "missing-hf-cache"
    broken_link = tmp_path / "huggingface"
    broken_link.symlink_to(missing_target, target_is_directory=True)

    monkeypatch.setenv("HF_HOME", str(broken_link))
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setattr(local_llm, "_model", None)
    monkeypatch.setattr(local_llm, "_tokenizer", None)
    monkeypatch.setattr(local_llm, "_loaded_name", None)
    monkeypatch.setattr(local_llm, "_loaded_adapter", None)
    monkeypatch.setattr(local_llm, "_cache_unavailable_logged", False)

    real_import = builtins.__import__
    mlx_imports: list[str] = []

    def guarded_import(name, *args, **kwargs):
        if name == "mlx_lm":
            mlx_imports.append(name)
            raise AssertionError("mlx_lm should not be imported for broken HF cache")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert local_llm._ensure_loaded() is False
    assert mlx_imports == []
