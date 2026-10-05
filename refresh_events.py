"""Refresh verified ETF calendars independently of the trading loop.

Only issuer-listed funds are supported; corporate earnings are never inferred.
A failed fetch retains the old verification date (seven-day expiry still applies).
An unparseable issuer document invalidates that provider's generated entries.
"""
import argparse
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URLS = {
    'spdr': 'https://www.ssga.com/library-content/products/fund-data/etfs/us/distribution/SPDR_Dividend_Distribution_Schedule.pdf',
    'ishares': 'https://www.ishares.com/us/literature/shareholder-letters/isharesandblackrocketfsdistributionschedule.pdf',
}
SYMBOLS = {
    'spdr': {'SPY', 'DIA', 'XLF', 'XLE', 'XLV', 'XLI', 'XLP', 'XLU', 'XLB', 'KRE', 'XBI', 'XOP'},
    'ishares': {'IWM', 'EEM', 'EFA', 'TLT', 'HYG', 'LQD', 'IGV'},
}


def parse_spdr(text, year):
    if f'Calendar Year {year}' not in text:
        raise ValueError('Issuer calendar does not cover the requested year')
    result = {}
    for block in re.split(r'Ex Date\s+Record Date\s+Payable Date', text)[1:]:
        symbols = set(re.findall(r'\(([A-Z]+)\)', block)) & SYMBOLS['spdr']
        if not symbols:
            continue
        values = re.findall(r'^\s*(?:January|February|March|April|May|June|July|August|September|October|November|December|Potential Excise Distribution)\s+(\d+/\d+/\d{4})\s+\d+/\d+/\d{4}\s+\d+/\d+/\d{4}', block, re.M)
        dates = [datetime.strptime(v, '%m/%d/%Y').date() for v in values]
        for symbol in symbols:
            expected = 13 if symbol == 'DIA' else 5
            if len(dates) != expected or len(set(dates)) != expected or any(d.year != year for d in dates):
                raise ValueError('Incomplete or changed SPDR group: ' + symbol)
            if symbol in result:
                raise ValueError('Ambiguous SPDR symbol group')
            result[symbol] = sorted(d.isoformat() for d in dates)
    if set(result) != SYMBOLS['spdr']:
        raise ValueError('Missing issuer membership for SPDR funds')
    return result


def parse_ishares(text, year):
    result = {}
    # Layout extraction keeps each schedule followed by its explicit fund list.
    blocks = re.split(r'(?m)^\s*(?=(?:WEEKLY|MONTHLY|QUARTERLY|SEMI-ANNUAL|ANNUAL) DISTRIBUTION\s)', text)
    for block in blocks[1:]:
        members = set(re.findall(r'\b([A-Z]+)\s+iShares\b', block)) & SYMBOLS['ishares']
        if not members:
            continue
        rows = re.findall(r'(?m)^\s*EX-DATE/RECORD DATE:\s*([^\n]+)', block)
        dates = [datetime.strptime(v, '%d-%b-%y').date() for row in rows for v in re.findall(r'\b\d{1,2}-[A-Z][a-z]{2}-\d{2}\b', row)]
        dates = [d for d in dates if d.year == year]
        for symbol in members:
            expected = 13 if symbol in {'TLT', 'HYG', 'LQD'} else 5 if symbol == 'IWM' else 3
            if len(dates) != expected or len(set(dates)) != expected:
                raise ValueError('Incomplete or changed iShares group: ' + symbol)
            if symbol in result:
                raise ValueError('Ambiguous iShares symbol group')
            result[symbol] = sorted(d.isoformat() for d in dates)
    if set(result) != SYMBOLS['ishares']:
        raise ValueError('Missing issuer membership for iShares funds')
    return result


def fetch(provider):
    request = Request(URLS[provider], headers={'User-Agent': 'MonitorG-calendar-maintenance/1.0', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=25) as response:
        data = response.read(5_000_001)
    if len(data) > 5_000_000 or not data.startswith(b'%PDF-'):
        raise ValueError('Invalid or oversized issuer PDF')
    return data


def extract(data):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'source.pdf'
        path.write_bytes(data)
        return subprocess.check_output(['/usr/bin/pdftotext', '-layout', str(path), '-'], timeout=20, text=True)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def refresh(path, today=None, fetcher=fetch, extractor=extract):
    today = today or datetime.now(ZoneInfo('America/New_York')).date()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        calendar = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(calendar, dict): raise ValueError('Calendar root must be an object')
        report = {'checked_at': datetime.now(ZoneInfo('America/New_York')).isoformat(), 'providers': {}}
        for provider, parser in [('spdr', parse_spdr), ('ishares', parse_ishares)]:
            try:
                data = fetcher(provider)
            except Exception as exc:
                report['providers'][provider] = {'status': 'fetch_failed', 'error': type(exc).__name__, 'note': 'Existing verification dates unchanged'}
                continue
            try:
                dates = parser(extractor(data), today.year)
            except Exception as exc:
                # A retrieved but unreadable/changed schedule cannot authorize entries.
                for symbol in SYMBOLS[provider]:
                    if isinstance(calendar.get(symbol), dict) and calendar[symbol].get('provider') == provider:
                        calendar[symbol]['verification_error'] = 'issuer_document_requires_review'
                report['providers'][provider] = {'status': 'review_required', 'error': str(exc)}
                continue
            digest = hashlib.sha256(data).hexdigest()
            for symbol, ex_dates in dates.items():
                calendar[symbol] = {
                    'verified_on': today.isoformat(), 'valid_through': date(today.year, 12, 31).isoformat(),
                    'earnings': [], 'ex_dividend': ex_dates, 'provider': provider,
                    'sources': [URLS[provider]], 'source_sha256': digest,
                    'basis': 'Issuer-listed ETF; corporate earnings not applicable. Published distribution schedule, including potential excise distributions. Dates remain subject to issuer revision.',
                }
            archive = path.parent / 'calendar_sources' / (provider+'-'+digest+'.pdf')
            archive.parent.mkdir(exist_ok=True)
            if not archive.exists(): archive.write_bytes(data)
            report['providers'][provider] = {'status': 'verified', 'symbols': sorted(dates), 'source_sha256': digest}
        atomic_json(path, calendar)
        atomic_json(path.with_name('calendar_refresh_status.json'), report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().with_name('events.json'))
    args = parser.parse_args()
    report = refresh(args.output)
    print(json.dumps(report, indent=2))
    return 0 if all(p['status'] == 'verified' for p in report['providers'].values()) else 1

if __name__ == '__main__':
    raise SystemExit(main())
