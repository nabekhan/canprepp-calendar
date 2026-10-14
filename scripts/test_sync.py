import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from icalendar import Calendar
import sync


def page(date, clocks=('19:30', '20:30'), zone='NST'):
    fields = [date.strftime('%B %d, %Y'), *clocks]
    if zone:
        fields.append(f'({zone})')
    markup = '<main data-elementor-type="single-post">'
    markup += ''.join(f'<div class="jet-listing-dynamic-field__content">{value}</div>' for value in fields)
    markup += '<a class="elementor-post-info__terms-list-item">General Surgery</a>'
    markup += '<h2>Online Event</h2><a href="https://example.org/meeting">Meeting Link</a>'
    markup += '<h2>Similar Upcoming Events</h2><div class="jet-listing-dynamic-field__content">09:00</div>'
    markup += '<a href="https://example.org/unrelated">Meeting Link</a></main>'
    return markup


def post(identifier=1, title='Town hall'):
    return {'id': identifier, 'title': {'rendered': title}, 'link': f'https://canprepp.ca/events/test-{identifier}/',
            'modified_gmt': '2026-10-12T00:00:00', 'program': [61]}


class CalendarTests(unittest.TestCase):
    def test_published_timezones_and_related_events(self):
        event = sync.parse_event(page(dt.date(2026, 10, 20)), post())
        self.assertEqual(event['start'], '2026-10-20T22:00:00+00:00')
        self.assertEqual(event['end'], '2026-10-20T23:00:00+00:00')
        self.assertEqual(event['links'], {'Meeting Link': 'https://example.org/meeting'})
        self.assertEqual(event['categories'], ['General Surgery'])
        self.assertEqual(event['location'], 'Online')

    def test_timezone_labels_follow_regional_rules_on_the_event_date(self):
        date = dt.date(2026, 7, 10)
        est = sync.parse_event(page(date, ('19:00',), 'EST'), post())
        edt = sync.parse_event(page(date, ('19:00',), 'EDT'), post())
        et = sync.parse_event(page(date, ('19:00',), 'ET'), post())
        self.assertEqual(est['start'], '2026-07-10T23:00:00+00:00')
        self.assertEqual(edt['start'], '2026-07-10T23:00:00+00:00')
        self.assertEqual(et['start'], edt['start'])
        for label in ('EST', 'EDT', 'ET'):
            winter = sync.parse_event(page(dt.date(2027, 1, 10), ('19:00',), label), post())
            self.assertEqual(winter['start'], '2027-01-11T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'Unknown published timezone'):
            sync.parse_event(page(date, ('19:00',), 'UNKNOWN'), post())

    def test_ubc_pacific_time_and_alberta_2026_rules(self):
        for date in (dt.date(2026, 10, 13), dt.date(2026, 11, 13), dt.date(2027, 1, 13)):
            for label in ('PST', 'PDT', 'PT', 'PCT'):
                event = sync.parse_event(page(date, ('17:00', '18:00'), label), post())
                start = dt.datetime.fromisoformat(event['start'])
                self.assertEqual(start, dt.datetime.combine(date + dt.timedelta(days=1), dt.time(0), sync.UTC))
                self.assertEqual(start.astimezone(sync.timezone('ABT')).hour, 18)
        ubc = sync.parse_event(page(dt.date(2026, 10, 13), ('17:00', '18:00'), 'PST'), post())
        mcmaster = sync.parse_event(page(dt.date(2026, 10, 13), ('18:00', '19:00'), 'EST'), post())
        self.assertEqual(dt.datetime.fromisoformat(ubc['start']).astimezone(sync.timezone('MT')).hour, 18)
        self.assertEqual(dt.datetime.fromisoformat(mcmaster['start']).astimezone(sync.timezone('MT')).hour, 16)

    def test_saskatchewan_and_fort_st_john_keep_their_local_rules(self):
        markup = page(dt.date(2026, 7, 10), ('19:00',), 'CST')
        sask = markup.replace('General Surgery</a>', 'University of Saskatchewan</a>')
        self.assertEqual(sync.parse_event(sask, post())['start'], '2026-07-11T01:00:00+00:00')
        self.assertEqual(sync.parse_event(markup, post())['start'], '2026-07-11T00:00:00+00:00')
        bc = page(dt.date(2026, 10, 13), ('19:00',), 'MST').replace('General Surgery</a>', 'University of British Columbia</a>')
        northern_bc = sync.parse_event(bc, post(title='UBC Rural Fort St John Site QA'))
        self.assertEqual(northern_bc['start'], '2026-10-14T02:00:00+00:00')

    def test_overnight_and_missing_end(self):
        event = sync.parse_event(page(dt.date(2026, 10, 20), ('11:30PM', '12:30 AM'), 'EDT'), post())
        self.assertEqual(event['start'], '2026-10-21T03:30:00+00:00')
        self.assertEqual(event['end'], '2026-10-21T04:30:00+00:00')
        self.assertIsNone(sync.parse_event(page(dt.date(2026, 10, 20), ('19:00',), 'EDT'), post())['end'])

    def test_all_day_when_time_is_not_published(self):
        event = sync.parse_event(page(dt.date(2026, 10, 20), (), ''), post())
        self.assertTrue(event['all_day'])
        self.assertEqual(event['start'], '2026-10-20')
        self.assertEqual(event['end'], '2026-10-21')

    def test_calendar_padding_keeps_correct_dates(self):
        month = dt.date(2026, 11, 1)
        start = dt.date(2026, 10, 26)
        cells = []
        for i in range(42):
            day = start + dt.timedelta(days=i)
            card = '<div data-post-id="15">Town hall</div>' if day == dt.date(2026, 11, 1) else ''
            cells.append(f'<td class="jet-calendar-week__day"><b class="jet-calendar-week__day-date">{day.day}</b>{card}</td>')
        markup = '<div class="jet-listing-calendar"><span class="jet-calendar-caption__name">November 2026</span>'
        markup += '<span class="jet-calendar-header__week-day">Mon</span><table><tr>' + ''.join(cells) + '</tr></table></div>'
        self.assertEqual(sync.calendar_entries(markup, month)['15']['date'], '2026-11-01')
        with self.assertRaisesRegex(RuntimeError, 'Wrong calendar month'):
            sync.calendar_entries(markup, dt.date(2026, 12, 1))

    def test_uid_sequence_utf8_and_unchanged_feed(self):
        value = sync.parse_event(page(dt.date(2026, 10, 20)), post(title='Séance, médecine; ' + 'é' * 100))
        old = sync.update_event(value, None, '2026-10-12T19:00:00+00:00')
        config = {'calendar_name': 'CANPREPP', 'refresh_hours': 3}
        raw = sync.make_calendar({'1': old}, config)
        unchanged = sync.update_event(value, old, '2026-10-12T22:00:00+00:00')
        self.assertEqual(raw, sync.make_calendar({'1': unchanged}, config))
        revised = sync.update_event({**value, 'title': 'New title'}, old, '2026-10-12T22:00:00+00:00')
        changed = Calendar.from_ical(sync.make_calendar({'1': revised}, config)).walk('VEVENT')[0]
        original = Calendar.from_ical(raw).walk('VEVENT')[0]
        self.assertEqual(changed['UID'], original['UID'])
        self.assertEqual(int(changed['SEQUENCE']), 1)
        self.assertEqual(str(original['SUMMARY']), value['title'])
        self.assertIn(b'X-PUBLISHED-TTL:PT3H', raw)
        self.assertTrue(all(len(line) <= 75 for line in raw.split(b'\r\n')))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'config.json').write_text(json.dumps({'calendar_name': 'Test', 'history_days': 30,
            'future_months': 12, 'program_ids': [], 'refresh_hours': 3, 'request_interval': 0}))
        self.date = dt.datetime.now(sync.UTC).date() + dt.timedelta(days=1)
        self.root_patch = patch.object(sync, 'ROOT', self.root)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)

    def execute(self, posts, cards, client):
        with patch.object(sync, 'Client', return_value=client), patch.object(sync, 'post_index', return_value=posts), \
             patch.object(sync, 'discover', return_value=cards), patch('builtins.print'):
            sync.run()

    def test_incremental_changes_and_deletion(self):
        client = Mock()
        client.get.return_value = (page(self.date).encode(), {})
        cards = {str(i): {'date': self.date.isoformat(), 'card': 'Town hall'} for i in (1, 2)}
        self.execute({str(i): post(i) for i in (1, 2)}, cards, client)
        self.assertEqual(client.get.call_count, 2)
        revised_post = post(title='Revised town hall')
        revised_post['modified_gmt'] = '2026-10-12T01:00:00'
        client.reset_mock()
        self.execute({'1': revised_post}, {'1': cards['1']}, client)
        self.assertEqual(client.get.call_count, 1)
        parsed = Calendar.from_ical((self.root / 'site/canprepp.ics').read_bytes()).walk('VEVENT')
        events = {str(e['UID']): e for e in parsed}
        self.assertEqual(str(events['canprepp-event-1@canprepp.ca']['SUMMARY']), 'Revised town hall')
        self.assertEqual(str(events['canprepp-event-2@canprepp.ca']['STATUS']), 'CANCELLED')
        self.assertEqual(int(events['canprepp-event-2@canprepp.ca']['SEQUENCE']), 1)
        before = (self.root / 'site/canprepp.ics').read_bytes()
        client.reset_mock()
        self.execute({'1': revised_post}, {'1': cards['1']}, client)
        self.assertEqual(client.get.call_count, 0)
        self.assertEqual(before, (self.root / 'site/canprepp.ics').read_bytes())

    def test_incomplete_or_unparseable_source_preserves_feed(self):
        client = Mock()
        client.get.return_value = (page(self.date).encode(), {})
        cards = {'1': {'date': self.date.isoformat(), 'card': 'Town hall'}}
        self.execute({'1': post()}, cards, client)
        before = (self.root / 'site/canprepp.ics').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'unexpectedly empty'):
            self.execute({'1': post()}, {}, client)
        revised = post(); revised['modified_gmt'] = '2026-10-12T02:00:00'
        client.get.return_value = (page(self.date, zone='UNKNOWN').encode(), {})
        with self.assertRaisesRegex(RuntimeError, 'Unknown published timezone'):
            self.execute({'1': revised}, cards, client)
        self.assertEqual(before, (self.root / 'site/canprepp.ics').read_bytes())

    def test_existing_cached_times_are_corrected_without_refetching(self):
        client = Mock()
        client.get.return_value = (page(self.date, ('17:00', '18:00'), 'PST').encode(), {})
        cards = {'1': {'date': self.date.isoformat(), 'card': 'Town hall'}}
        self.execute({'1': post()}, cards, client)
        state = json.loads((self.root / 'data/state.json').read_text())
        legacy = state['cache']['1']['event']
        for field in ('start', 'end'):
            legacy[field] = (dt.datetime.fromisoformat(legacy[field]) + dt.timedelta(hours=1)).isoformat()
        state['events']['1'] = sync.update_event(legacy, None, '2026-10-12T19:00:00+00:00')
        (self.root / 'data/state.json').write_text(json.dumps(state))
        (self.root / 'site/canprepp.ics').write_bytes(sync.make_calendar(state['events'], {'calendar_name': 'Test', 'refresh_hours': 3}))
        client.reset_mock()
        self.execute({'1': post()}, cards, client)
        self.assertEqual(client.get.call_count, 0)
        event = Calendar.from_ical((self.root / 'site/canprepp.ics').read_bytes()).walk('VEVENT')[0]
        self.assertEqual(str(event['UID']), 'canprepp-event-1@canprepp.ca')
        self.assertEqual(int(event['SEQUENCE']), 1)
        self.assertEqual(event.decoded('DTSTART').astimezone(sync.timezone('PT')).hour, 17)
        before = (self.root / 'site/canprepp.ics').read_bytes()
        self.execute({'1': post()}, cards, client)
        self.assertEqual(before, (self.root / 'site/canprepp.ics').read_bytes())


if __name__ == '__main__':
    unittest.main()
