"""Replay ordinary burst input against real providers, with no LINE delivery."""
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
def run_replay():
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))
    message = sys.stdin.readline().strip()
    if not message:
        raise SystemExit('missing fixture')
    with tempfile.TemporaryDirectory(prefix='line-reply-replay-') as temporary:
        os.environ['SQLITE_PATH'] = str(Path(temporary) / 'isolated.sqlite3')
        os.environ['BOT_MUTED'] = 'true'
        import main
        import burst_filter
        import gemini_client
        import claude_client
        import notify_discord
        import fulltext_fetcher
        logging.disable(logging.CRITICAL)
        main._configure_local_text_llm_runtime()
        main._load_quota_state()
        # Match current quota state but keep replay cooldown bookkeeping isolated.
        quota_copy = Path(temporary) / 'quota.json'
        if Path(main._QUOTA_STATE_FILE).exists():
            quota_copy.write_bytes(Path(main._QUOTA_STATE_FILE).read_bytes())
        main._QUOTA_STATE_FILE = str(quota_copy)
        result = {'conditions': 'ordinary sender, no conversation history, empty fact cache',
                  'heuristic': burst_filter._heuristic_decision(message), 'provider_attempts': [],
                  'reply': '', 'line_sent': False}
        real_collect = main._collect_web_research_sources
        def observed_collect(text):
            rows = real_collect(text)
            result['research'] = {'query': text, 'source_count': len(rows),
                'sources': [{'title': r.get('title'), 'url': r.get('url'), 'published': r.get('published'),
                             'kind': r.get('evidence_kind', 'snippet'),
                             'body': (r.get('full_text') or r.get('snippet') or '')[:700]} for r in rows]}
            return rows
        def capture_reply(token, text, **kwargs):
            rendered = main._prepare_outbound_text(text, source='isolated-replay')
            if rendered and not main._is_system_status_outbound(rendered):
                result['reply'] = rendered[:4900] + main._get_quota_footer()
            return False
        def deny_delivery(*args, **kwargs):
            raise AssertionError('LINE transport forbidden during replay')
        def provider_wrapper(name, function):
            def wrapped(*args, **kwargs):
                attempt = {'provider': name}
                result['provider_attempts'].append(attempt)
                try:
                    reply = function(*args, **kwargs)
                    attempt['produced_text'] = bool(reply)
                    return reply
                except Exception as exc:
                    attempt['error_type'] = type(exc).__name__
                    raise
            return wrapped
        with contextlib.ExitStack() as stack:
            patches = [
                patch.object(main, "_collect_web_research_sources", observed_collect),
                patch.object(main, '_reply', capture_reply),
                patch.object(fulltext_fetcher, 'DEFAULT_CACHE_DB', Path(temporary) / 'web_cache.sqlite3'),
                patch.object(main, 'ApiClient', deny_delivery),
                patch.object(main, '_thinking_indicator', lambda *_: contextlib.nullcontext()),
                patch.object(main, '_gemini_side_task_allowed', lambda *_: False),
                patch.object(main, '_maybe_extract_facts', lambda *_: None),
                patch.object(main, '_maybe_capture_calendar_event', lambda *a, **k: None),
                patch.object(main, '_get_persona_notes', lambda *_: []),
                patch.object(main.memory, 'get_context', lambda *_: []),
                patch.object(main.memory, 'top_facts', lambda *a, **k: []),
                patch.object(main.memory, 'list_filter_rules', lambda *_: []),
                patch.object(main.memory, 'check_fact_cache', lambda *a: None),
                patch.object(main.memory, 'store_fact_cache', lambda *a: None),
                patch.object(main.memory, 'append_turn', lambda *a: None),
                patch.object(main.memory, 'mark_inbound_events_completed_no_reply', lambda *a: None),
                patch.object(gemini_client, '_log_quality_violation', lambda *a: None),
                patch.object(gemini_client, '_alert_quality_violation', lambda *a: None),
                patch.object(notify_discord, 'send_dm', lambda *a, **k: False),
                patch.object(claude_client, 'chat', provider_wrapper('claude', claude_client.chat)),
                patch.object(gemini_client, 'chat', provider_wrapper('gemini', gemini_client.chat)),
            ]
            for item in patches:
                stack.enter_context(item)
            burst_filter._classify_and_maybe_respond(
                'G_REPLAY', [('M_REPLAY', message, 'U_REPLAY', time.time())], 'T_REPLAY')
            if 'G_REPLAY' in burst_filter._waiting_groups:
                burst_filter._flush_burst('G_REPLAY', force_respond=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run_replay()
