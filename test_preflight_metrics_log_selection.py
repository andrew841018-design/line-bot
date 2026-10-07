import os

import preflight_check as pf


def test_metrics_port_uses_newer_stdout_log_after_launchd_log_migration(
    tmp_path, monkeypatch
):
    legacy_log = tmp_path / "cloudflared.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    stdout_log.write_text(
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    os.utime(legacy_log, (1_000, 1_000))
    os.utime(stdout_log, (2_000, 2_000))
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() == 53459


def test_metrics_port_falls_back_to_legacy_log(tmp_path, monkeypatch):
    legacy_log = tmp_path / "cloudflared.log"
    legacy_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(
        pf, "CLOUDFLARED_STDOUT_LOG", tmp_path / "missing-stdout.log"
    )

    assert pf._cloudflared_metrics_port() == 41001


def test_metrics_port_fails_closed_when_newer_log_has_no_port(
    tmp_path, monkeypatch
):
    legacy_log = tmp_path / "cloudflared.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    stdout_log.write_text("2026-08-25T02:21:08Z INF tunnel startup pending\n")
    os.utime(legacy_log, (1_000, 1_000))
    os.utime(stdout_log, (2_000, 2_000))
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() is None


def test_metrics_port_uses_newer_legacy_log(tmp_path, monkeypatch):
    legacy_log = tmp_path / "cloudflared.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    stdout_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    os.utime(stdout_log, (1_000, 1_000))
    os.utime(legacy_log, (2_000, 2_000))
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() == 41001


def test_metrics_port_prefers_stdout_on_equal_mtime(tmp_path, monkeypatch):
    legacy_log = tmp_path / "cloudflared.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    stdout_log.write_text(
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    os.utime(legacy_log, (2_000, 2_000))
    os.utime(stdout_log, (2_000, 2_000))
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() == 53459


def test_metrics_port_uses_last_match_in_authoritative_log(
    tmp_path, monkeypatch
):
    stdout_log = tmp_path / "cloudflared_stdout.log"
    stdout_log.write_text(
        "2026-08-25T02:20:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    monkeypatch.setattr(
        pf, "CLOUDFLARED_LOG", tmp_path / "missing-legacy.log"
    )
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() == 53459


def test_metrics_port_rejects_symlinked_authoritative_log(
    tmp_path, monkeypatch
):
    legacy_log = tmp_path / "cloudflared.log"
    target = tmp_path / "stdout-target.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    target.write_text(
        "2026-08-25T02:21:08Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    stdout_log.symlink_to(target)
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() is None


def test_metrics_port_rejects_non_regular_authoritative_log(
    tmp_path, monkeypatch
):
    legacy_log = tmp_path / "cloudflared.log"
    stdout_log = tmp_path / "cloudflared_stdout.log"
    legacy_log.write_text(
        "2026-08-24T06:58:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
    )
    stdout_log.mkdir()
    monkeypatch.setattr(pf, "CLOUDFLARED_LOG", legacy_log)
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() is None


def test_metrics_port_rejects_previous_generation_request_in_same_log(
    tmp_path, monkeypatch
):
    stdout_log = tmp_path / "cloudflared_stdout.log"
    stdout_log.write_text(
        "2026-08-25T02:20:00Z INF Generated Connector ID: old\n"
        "2026-08-25T02:20:01Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
        "2026-08-25T02:21:04Z INF Requesting new quick Tunnel on "
        "trycloudflare.com...\n"
    )
    monkeypatch.setattr(
        pf, "CLOUDFLARED_LOG", tmp_path / "missing-legacy.log"
    )
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() is None


def test_metrics_port_rejects_previous_connector_generation_in_same_log(
    tmp_path, monkeypatch
):
    stdout_log = tmp_path / "cloudflared_stdout.log"
    stdout_log.write_text(
        "2026-08-25T02:20:00Z INF Generated Connector ID: old\n"
        "2026-08-25T02:20:01Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
        "2026-08-25T02:21:08Z INF Generated Connector ID: new\n"
    )
    monkeypatch.setattr(
        pf, "CLOUDFLARED_LOG", tmp_path / "missing-legacy.log"
    )
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() is None


def test_metrics_port_accepts_latest_metrics_after_new_generation_markers(
    tmp_path, monkeypatch
):
    stdout_log = tmp_path / "cloudflared_stdout.log"
    stdout_log.write_text(
        "2026-08-25T02:20:00Z INF Starting metrics server on "
        "127.0.0.1:41001/metrics\n"
        "2026-08-25T02:21:04Z INF Requesting new quick Tunnel on "
        "trycloudflare.com...\n"
        "2026-08-25T02:21:08Z INF Generated Connector ID: new\n"
        "2026-08-25T02:21:09Z INF Starting metrics server on "
        "127.0.0.1:53459/metrics\n"
    )
    monkeypatch.setattr(
        pf, "CLOUDFLARED_LOG", tmp_path / "missing-legacy.log"
    )
    monkeypatch.setattr(pf, "CLOUDFLARED_STDOUT_LOG", stdout_log)

    assert pf._cloudflared_metrics_port() == 53459
