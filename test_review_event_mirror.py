import pytest

import calendar_db
import memory


def _seed_event():
    event_id = calendar_db.insert_event(
        'GROUP_SYNTHETIC', 'Review fixture', '2099-11-16', '14:30',
        participants=['Guest'],
    )
    assert event_id
    return calendar_db.get_active_event_by_id('GROUP_SYNTHETIC', event_id)


def _mirror(event):
    with memory._conn() as conn:
        return conn.execute(
            'SELECT * FROM reminders WHERE group_id=? AND source_ref=?',
            (event['group_id'], event['event_id']),
        ).fetchall()


def test_stale_snapshot_cannot_undo_corrected_reminder():
    event = _seed_event()
    result = calendar_db.correct_event_and_reminder_by_id(
        event['group_id'], event['event_id'], new_date='2099-11-18',
        new_time='15:30', new_title=None,
    )
    assert result['status'] == 'updated'
    before = _mirror(event)
    assert not calendar_db.synchronize_pending_event_reminder_mirror(event)
    assert _mirror(event) == before
    fresh = calendar_db.get_active_event_by_id(event['group_id'], event['event_id'])
    assert calendar_db.synchronize_pending_event_reminder_mirror(fresh)
    assert _mirror(event) == before


@pytest.mark.parametrize(('column', 'value'), [
    ('title', 'Updated fixture'), ('event_date', '2099-11-18'),
    ('event_time', '15:30'), ('location', 'Room B'),
    ('participants', '["New guest"]'), ('source_msg_id', 'synthetic-message'),
    ('event_type', 'medical'), ('reminder_lead_days', 3),
    ('status', 'cancelled'),
])
def test_changed_source_payload_rejects_stale_mirror(column, value):
    event = _seed_event()
    before = _mirror(event)
    with calendar_db._conn() as conn:
        # Column values come solely from the fixed synthetic parameter list.
        conn.execute(f'UPDATE events SET {column}=? WHERE event_id=?',
                     (value, event['event_id']))
    assert not calendar_db.synchronize_pending_event_reminder_mirror(event)
    assert _mirror(event) == before


def test_delivery_flags_do_not_invalidate_snapshot_or_get_reset():
    event = _seed_event()
    with calendar_db._conn() as conn:
        conn.execute('UPDATE events SET reminded_1d=123 WHERE event_id=?',
                     (event['event_id'],))
        conn.execute('UPDATE reminders SET last_pushed_at=123, weekly_count=4 '
                     'WHERE source_ref=?', (event['event_id'],))
    before = _mirror(event)
    event['participants'] = ['Guest']
    assert calendar_db.synchronize_pending_event_reminder_mirror(event)
    assert _mirror(event) == before
    with calendar_db._conn() as conn:
        assert conn.execute('SELECT reminded_1d FROM events WHERE event_id=?',
                            (event['event_id'],)).fetchone() == (123,)


@pytest.mark.parametrize('status', ['cancelled', 'done', 'expired'])
def test_source_sync_preserves_terminal_reminder(status):
    event = _seed_event()
    with memory._conn() as conn:
        conn.execute('UPDATE reminders SET status=? WHERE source_ref=?',
                     (status, event['event_id']))
    before = _mirror(event)
    assert not calendar_db.synchronize_pending_event_reminder_mirror(event)
    assert _mirror(event) == before
