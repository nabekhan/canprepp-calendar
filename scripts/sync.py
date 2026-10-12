#!/usr/bin/env python3
"""Mirror CANPREPP's public residency calendar to a subscribable ICS feed.

Run `python scripts/sync.py` to sync, or add --refresh to reload cached event pages.
The sync uses the site's own calendar navigation endpoint and public post index.
Published timezone abbreviations are interpreted literally, without guessing DST.
"""
import argparse
import calendar as month_names
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vDuration

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://canprepp.ca/'
UTC = dt.timezone.utc
OFFSETS = {'UTC': 0, 'GMT': 0, 'EST': -300, 'EDT': -240, 'CST': -360, 'CDT': -300,
           'MST': -420, 'MDT': -360, 'PST': -480, 'PDT': -420, 'AST': -240, 'ADT': -180,
           'NST': -210, 'NDT': -150, 'HNE': -300, 'HAE': -240, 'HNC': -360, 'HAC': -300,
           'HNR': -420, 'HAR': -360, 'HNP': -480, 'HAP': -420, 'HNA': -240, 'HAA': -180,
           'HNT': -210, 'HAT': -150}
REGIONS = {'ET': 'America/Toronto', 'CT': 'America/Winnipeg', 'MT': 'America/Edmonton',
           'PT': 'America/Vancouver', 'AT': 'America/Halifax', 'NT': 'America/St_Johns'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, data):
    if path.exists() and path.read_bytes() == data: return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as f: f.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)
    return True


class Client:
    def __init__(self, interval=1.1): self.interval, self.last = interval, 0
    def get(self, url, form=None):
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        for attempt in range(5):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            req = urllib.request.Request(url, data=body, headers={
                'User-Agent': 'CANPREPPCalendarSync/1.0 (https://github.com/nabekhan/canprepp-calendar)',
                'Referer': BASE, 'Cache-Control': 'no-cache'})
            try:
                with urllib.request.urlopen(req, timeout=45) as r: return r.read(), r.headers
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504): raise
            except (urllib.error.URLError, TimeoutError): pass
            time.sleep(min(30, 2 ** attempt))
        raise RuntimeError('CANPREPP request failed after retries. Previous feed preserved.')


def month_shift(date, amount):
    n = date.year * 12 + date.month - 1 + amount
    return dt.date(n // 12, n % 12 + 1, 1)


def post_index(client):
    result = {}
    total = pages = None
    page = 1
    while pages is None or page <= pages:
        query = {'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc',
                 '_fields': 'id,modified_gmt,title,link,program,university,sub-specialty,match-type'}
        raw, headers = client.get(BASE + 'wp-json/wp/v2/events?' + urllib.parse.urlencode(query))
        rows = json.loads(raw)
        if total is None: total, pages = int(headers['X-WP-Total']), int(headers['X-WP-TotalPages'])
        if int(headers['X-WP-Total']) != total or int(headers['X-WP-TotalPages']) != pages:
            raise RuntimeError('Source post count changed during pagination. Retry sync.')
        if len(rows) != min(100, total - (page - 1) * 100): raise RuntimeError('Incomplete source post page.')
        for row in rows:
            key = str(row['id'])
            if key in result: raise RuntimeError('Overlapping source post pages.')
            result[key] = row
        page += 1
    if len(result) != total: raise RuntimeError('Incomplete public event index.')
    print(f'Verified public index: {total} event posts', flush=True)
    return result


def flatten_form(key, value, pairs):
    if isinstance(value, dict):
        for k, v in value.items(): flatten_form(f'{key}[{k}]', v, pairs)
    elif isinstance(value, list):
        for i, v in enumerate(value): flatten_form(f'{key}[{i if isinstance(v, (dict, list)) else ""}]', v, pairs)
    else:
        pairs.append((key, '' if value is None else str(value).lower() if isinstance(value, bool) else str(value)))


def calendar_entries(markup, month):
    soup = BeautifulSoup(markup, 'html.parser')
    calendar = soup.select_one('.jet-listing-calendar')
    if not calendar: raise RuntimeError(f'Calendar markup missing for {month}.')
    expected = f'{month_names.month_name[month.month]} {month.year}'
    caption = calendar.select_one('.jet-calendar-caption__name')
    if not caption or caption.get_text(' ', strip=True) != expected:
        raise RuntimeError(f'Wrong calendar month returned for {expected}.')
    headings = [x.get_text(strip=True) for x in calendar.select('.jet-calendar-header__week-day')]
    if headings[:1] == ['Mon']: start = month - dt.timedelta(days=month.weekday())
    elif headings[:1] == ['Sun']: start = month - dt.timedelta(days=(month.weekday() + 1) % 7)
    else: raise RuntimeError('Unrecognized calendar week layout.')
    result = {}
    cells = [c for c in calendar.select('td.jet-calendar-week__day') if c.select_one('.jet-calendar-week__day-date')]
    if len(cells) not in (28, 35, 42): raise RuntimeError('Incomplete calendar grid.')
    for i, cell in enumerate(cells):
        date = start + dt.timedelta(days=i)
        if int(cell.select_one('.jet-calendar-week__day-date').get_text(strip=True)) != date.day:
            raise RuntimeError('Calendar day alignment failed.')
        for card in cell.select('[data-post-id]'):
            key = card['data-post-id']
            value = {'date': date.isoformat(), 'card': card.get_text(' ', strip=True)}
            if key in result and result[key] != value: raise RuntimeError(f'Multiple dates for event {key}; recurrence needs review.')
            result[key] = value
    return result


def discover(client, start, end):
    raw, _ = client.get(BASE)
    home = raw.decode('utf-8')
    soup = BeautifulSoup(home, 'html.parser')
    widget = soup.select_one('.jet-listing-calendar[data-settings]')
    if not widget: raise RuntimeError('Public calendar widget not found.')
    settings = json.loads(widget['data-settings'])
    settings['renderer'] = widget.get('data-renderer', '')
    settings['__switch_direction'] = 1
    current_caption = widget.select_one('.jet-calendar-caption__name').get_text(' ', strip=True)
    result = {}
    month = start.replace(day=1)
    while month <= end:
        label = f'{month_names.month_name[month.month]} {month.year}'
        if label == current_caption: markup = home
        else:
            pairs = []
            for k, v in {'jet_engine_action': 'jet_engine_calendar_get_month', 'month': label,
                         'settings': settings, 'post': widget['data-post'], 'calendar_type': 'default'}.items():
                flatten_form(k, v, pairs)
            raw, _ = client.get(BASE + '?nocache=1', pairs)
            response = json.loads(raw)
            if response.get('success') is not True: raise RuntimeError(f'Calendar request failed for {label}.')
            markup = response['data']['content']
        entries = calendar_entries(markup, month)
        for key, value in entries.items():
            if not start <= dt.date.fromisoformat(value['date']) <= end: continue
            if key in result and result[key] != value: raise RuntimeError(f'Calendar changed while reading event {key}.')
            result[key] = value
        print(f'Calendar {label}: {len(entries)} event entries', flush=True)
        month = month_shift(month, 1)
    return result


def timezone(label):
    label = label.strip()
    if label in OFFSETS: return dt.timezone(dt.timedelta(minutes=OFFSETS[label]), label)
    if label in REGIONS: return ZoneInfo(REGIONS[label])
    try: return ZoneInfo(label)
    except Exception: raise ValueError('Unknown published timezone: ' + label) from None


def parse_clock(value):
    value = re.sub(r'\s+', ' ', value).upper().strip()
    value = re.sub(r'\s*([AP]M)$', r' \1', value)
    return dt.datetime.strptime(value, '%I:%M %p' if value.endswith(('AM', 'PM')) else '%H:%M').time()


def parse_event(markup, post):
    soup = BeautifulSoup(markup, 'html.parser')
    root = soup.select_one('[data-elementor-type="single-post"]')
    if not root: raise ValueError('Single event template missing.')
    similar = root.find(lambda t: t.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and 'Similar Upcoming Events' in t.get_text())
    if similar: root = BeautifulSoup(str(root).split(str(similar), 1)[0], 'html.parser')
    values = [re.sub(r'\s+', ' ', t.get_text(' ', strip=True).replace('\xa0', ' ')).strip() for t in root.select('.jet-listing-dynamic-field__content')]
    date = None
    for value in values:
        for fmt in ('%B %d, %Y', '%B %d %Y'):
            try: date = dt.datetime.strptime(value, fmt).date(); break
            except ValueError: pass
        if date: break
    if not date: raise ValueError('Published event date missing.')
    clocks = []
    zone = ''
    for value in values:
        clocks.extend(re.findall(r'\b\d{1,2}:\d{2}(?:\s*[AP]M)?\b', value, re.I))
        match = re.fullmatch(r'\(([^)]+)\)', value)
        if match and not zone: zone = match[1].strip()
    if clocks:
        if not zone: raise ValueError('Published event timezone missing.')
        tz = timezone(zone)
        start = dt.datetime.combine(date, parse_clock(clocks[0]), tz)
        end = dt.datetime.combine(date, parse_clock(clocks[1]), tz) if len(clocks) > 1 else None
        if end and end < start: end += dt.timedelta(days=1)
        if end == start: end = None
        start_text = start.astimezone(UTC).isoformat()
        end_text = end.astimezone(UTC).isoformat() if end else None
        all_day = False
    else:
        start_text, end_text, all_day = date.isoformat(), (date + dt.timedelta(days=1)).isoformat(), True
    terms = list(dict.fromkeys(t.get_text(' ', strip=True) for t in root.select('.elementor-post-info__terms-list-item')))
    links = {}
    for a in root.select('a[href]'):
        label = a.get_text(' ', strip=True)
        if re.search(r'meeting link|register|registration|rsvp|join', label, re.I) and a['href'].startswith(('https://', 'http://')):
            links[label] = a['href']
    headings = [h.get_text(' ', strip=True) for h in root.select('h1,h2,h3,h4,h5,h6')]
    online = any(h.lower() == 'online event' for h in headings)
    title = BeautifulSoup(html.unescape(post['title']['rendered']), 'html.parser').get_text(' ', strip=True)
    status = 'CANCELLED' if re.search(r'\b(cancelled|canceled|annulée?|annulé)\b', title, re.I) else 'CONFIRMED'
    return {'id': post['id'], 'title': title, 'url': post['link'], 'date': date.isoformat(),
            'start': start_text, 'end': end_text, 'all_day': all_day, 'timezone_label': zone,
            'published_time': ' – '.join(clocks[:2]) + (' ' + zone if zone else ''),
            'categories': terms, 'links': links, 'location': 'Online' if online else '', 'status': status}


def update_event(event, previous, now):
    fingerprint = digest(event)
    if previous and previous.get('fingerprint') == fingerprint: return previous
    return {**event, 'fingerprint': fingerprint, 'sequence': previous.get('sequence', 0) + 1 if previous else 0,
            'created': previous.get('created', now) if previous else now, 'last_modified': now}


def make_calendar(events, config):
    cal = Calendar()
    cal.add('prodid', '-//nabekhan//CANPREPP Calendar Sync//EN')
    cal.add('version', '2.0'); cal.add('calscale', 'GREGORIAN'); cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', config['calendar_name'])
    cal.add('x-published-ttl', vDuration(dt.timedelta(hours=config['refresh_hours'])))
    cal.add('refresh-interval', dt.timedelta(hours=config['refresh_hours']), parameters={'VALUE': 'DURATION'})
    for row in sorted(events.values(), key=lambda e: (e['start'], e['id'])):
        event = Event()
        event.add('uid', f'canprepp-event-{row["id"]}@canprepp.ca')
        event.add('summary', row['title']); event.add('url', row['url']); event.add('status', row['status'])
        parser = dt.date.fromisoformat if row['all_day'] else dt.datetime.fromisoformat
        event.add('dtstart', parser(row['start']))
        if row.get('end'): event.add('dtend', parser(row['end']))
        event.add('dtstamp', dt.datetime.fromisoformat(row['last_modified']))
        event.add('last-modified', dt.datetime.fromisoformat(row['last_modified']))
        event.add('created', dt.datetime.fromisoformat(row['created'])); event.add('sequence', row['sequence'])
        if row['location']: event.add('location', row['location'])
        if row['categories']: event.add('categories', row['categories'])
        description = [row['url']]
        if row['published_time']: description.append('Published time: ' + row['published_time'])
        description.extend(f'{label}: {url}' for label, url in row['links'].items())
        event.add('description', '\n'.join(description)); event.add('x-canprepp-id', str(row['id']))
        cal.add_component(event)
    raw = cal.to_ical()
    parsed = Calendar.from_ical(raw)
    components = parsed.walk('VEVENT')
    uids = [str(c['UID']) for c in components]
    if len(components) != len(events) or len(set(uids)) != len(uids): raise RuntimeError('ICS round-trip validation failed.')
    for component in components:
        if component.errors: raise RuntimeError('Invalid ICS event.')
        if 'DTEND' in component and component.decoded('DTEND') <= component.decoded('DTSTART'):
            raise RuntimeError('ICS end is not after start.')
    return raw


def run(refresh=False):
    config = json.loads((ROOT / 'config.json').read_text())
    client = Client(config['request_interval'])
    now = dt.datetime.now(UTC); stamp = now.replace(microsecond=0).isoformat()
    start = now.date() - dt.timedelta(days=config['history_days'])
    end = month_shift(now.date(), config['future_months'] + 1) - dt.timedelta(days=1)
    state_path = ROOT / 'data/state.json'
    old = json.loads(state_path.read_text()) if state_path.exists() else {'cache': {}, 'events': {}}
    pending_path = ROOT / '.cache/event-cache.json'
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    posts = post_index(client)
    discovered = discover(client, start, end)
    if not discovered: raise RuntimeError('Calendar unexpectedly empty. Previous feed preserved.')
    if not discovered.keys() <= posts.keys(): raise RuntimeError('Calendar/index mismatch. Retry sync.')
    if config['program_ids']:
        discovered = {key: v for key, v in discovered.items() if set(posts[key].get('program', [])) & set(config['program_ids'])}
    cache = {**old['cache'], **pending}; events = {}; fetched = 0
    for key, card in sorted(discovered.items(), key=lambda item: int(item[0])):
        post, previous = posts[key], cache.get(key)
        stale = not previous or (now - dt.datetime.fromisoformat(previous['fetched_at'])).total_seconds() > 7 * 86400
        if refresh or stale or previous.get('modified_gmt') != post['modified_gmt'] or previous.get('calendar_hash') != digest(card):
            raw, _ = client.get(post['link'])
            try: value = parse_event(raw.decode('utf-8'), post)
            except Exception as e: raise RuntimeError(f'Event {key} ({post["link"]}): {e}') from e
            if value['date'] != card['date']: raise RuntimeError(f'Event {key} date differs between calendar and page.')
            cache[key] = {'modified_gmt': post['modified_gmt'], 'calendar_hash': digest(card), 'fetched_at': stamp, 'event': value}
            pending[key] = cache[key]
            atomic_write(pending_path, json.dumps(pending, ensure_ascii=False).encode())
            fetched += 1
            print(f'Fetched event {key}: {value["title"]}', flush=True)
        value = cache[key]['event']
        events[key] = update_event(value, old['events'].get(key), stamp)
    # A removed public post gets the same UID with an increased cancellation sequence.
    for key, previous in old['events'].items():
        if key not in posts and start <= dt.date.fromisoformat(previous['date']) <= end:
            value = {k: v for k, v in previous.items() if k not in ('fingerprint', 'sequence', 'created', 'last_modified')}
            value['status'] = 'CANCELLED'; events[key] = update_event(value, previous, stamp)
    raw = make_calendar(events, config)
    changed = atomic_write(ROOT / 'site/canprepp.ics', raw)
    active_cache = {key: value for key, value in cache.items() if key in discovered}
    state = {'cache': active_cache, 'events': events}
    atomic_write(state_path, (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode())
    pending_path.unlink(missing_ok=True)
    status = {'checked_at': stamp, 'source_event_posts': len(posts), 'event_count': len(events), 'pages_fetched': fetched,
              'feed_changed': changed, 'range_start': start.isoformat(), 'range_end': end.isoformat()}
    for path in (ROOT / 'data/sync-status.json', ROOT / 'site/sync-status.json'):
        atomic_write(path, (json.dumps(status, indent=2) + '\n').encode())
    atomic_write(ROOT / 'site/.nojekyll', b'')
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Reload every event page instead of reusing metadata.')
    args = parser.parse_args()
    try: run(args.refresh)
    except Exception as e:
        print('Sync failed; published feed was not replaced: ' + str(e), file=sys.stderr); sys.exit(1)
