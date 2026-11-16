#!/usr/bin/env python3
"""Manual-only 24-hour SSCOFS wind-forcing pilot; never publishes live data.

Run in existing Bellingham Bay repository next to update_sscofs.py and
 test_sscofs_wind.py. Downloads nine operational fields files (f003..f027).
"""
import argparse
import datetime as dt
import json
import math
import sys
import tempfile
from pathlib import Path

import update_sscofs as currents
import test_sscofs_wind as wind

UTC = dt.timezone.utc
HOURS = tuple(range(3, 28, 3))
EXPECTED_NAMES = [entry[0] for entry in wind.SITES]


def parse_utc(s):
    value = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    if value.utcoffset() is None:
        raise ValueError('Timestamp missing timezone')
    return value.astimezone(UTC)


def validate_timeline(samples, hours=HOURS):
    if len(samples) != len(hours):
        raise ValueError(f'Expected {len(hours)} forecast samples; got {len(samples)}')
    cycle = parse_utc(samples[0]['modelCycleUTC'])
    for index, (sample, hour) in enumerate(zip(samples, hours)):
        if parse_utc(sample['modelCycleUTC']) != cycle:
            raise ValueError('Mixed NOAA model cycles')
        valid = parse_utc(sample['modelValidUTC'])
        expected = cycle + dt.timedelta(hours=hour)
        if abs((valid - expected).total_seconds()) > 900:
            raise ValueError(f'f{hour:03d} timestamp mismatch: {valid} != {expected}')
        locations = sample['locations']
        if [row['location'] for row in locations] != EXPECTED_NAMES:
            raise ValueError('Unexpected location order or missing locations')
        for row in locations:
            if not row.get('available'):
                raise ValueError(f"f{hour:03d}: no wind at {row['location']}")
            speed = row['windSpeedMph']
            direction = row['windFromDegrees']
            if not math.isfinite(speed) or not 0 <= speed <= 180:
                raise ValueError('Invalid modeled wind speed')
            if not math.isfinite(direction) or not 0 <= direction <= 360:
                raise ValueError('Invalid modeled wind direction')
        if index and (valid - parse_utc(samples[index - 1]['modelValidUTC'])).total_seconds() != 10800:
            raise ValueError('Model times are not spaced three hours apart')
    return cycle


def sample_to_report(samples, cycle, hours):
    time_series = []
    for sample, hour in zip(samples, hours):
        by_location = {}
        for record in sample['locations']:
            by_location[record['location']] = {
                'speedMph': record['windSpeedMph'],
                'fromDegrees': record['windFromDegrees'],
                'sampleCells': record['sampleCells'],
                'nearestMeshCellKm': record['nearestMeshCellKm'],
            }
        time_series.append({
            'forecastHour': hour,
            'validUTC': sample['modelValidUTC'],
            'locations': by_location,
        })
    return {
        'status': 'pilot_only_not_published',
        'dataType': 'sscofs_24_hour_wind_forcing_timeline',
        'source': 'NOAA SSCOFS FVCOM model wind forcing',
        'modelCycleUTC': cycle.isoformat().replace('+00:00', 'Z'),
        'forecastHours': list(hours),
        'snapshotCount': len(time_series),
        'validStartUTC': time_series[0]['validUTC'],
        'validEndUTC': time_series[-1]['validUTC'],
        'locationNames': EXPECTED_NAMES,
        'windUnits': 'mph',
        'windDirectionConvention': 'meteorological FROM degrees true',
        'snapshots': time_series,
        'notes': [
            'Model wind forcing, NOT station measurements or an independently validated local forecast.',
            'Does not contain observed wind gusts or wave heights.',
            'Three-hour sampling of a 24-hour span; do not use as navigational or on-water safety guidance.',
        ],
    }


def self_test():
    cycle = dt.datetime(2026, 11, 14, 21, tzinfo=UTC)
    samples = []
    for hour in HOURS:
        samples.append({
            'modelCycleUTC': cycle.isoformat().replace('+00:00', 'Z'),
            'modelValidUTC': (cycle + dt.timedelta(hours=hour)).isoformat().replace('+00:00', 'Z'),
            'locations': [{'location': name, 'available': True, 'windSpeedMph': 20.0,
                           'windFromDegrees': 140, 'sampleCells': 60, 'nearestMeshCellKm': .15}
                          for name in EXPECTED_NAMES],
        })
    assert validate_timeline(samples) == cycle
    assert len(sample_to_report(samples, cycle, HOURS)['snapshots']) == 9
    bad = json.loads(json.dumps(samples))
    bad[4]['modelValidUTC'] = bad[3]['modelValidUTC']
    try:
        validate_timeline(bad)
        raise AssertionError('Expected timestamp test failure')
    except ValueError:
        pass
    print('PASS: 9 snapshots, model cycles, 3-hour time spacing, wind vectors, invalid timestamp rejection')


def run(output):
    errors = []
    now = dt.datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix='bellingham-wind-') as folder:
        for date, hour_of_cycle in currents.candidate_runs(now):
            cyc = dt.datetime.strptime(date + f'{hour_of_cycle:02d}', '%Y%m%d%H').replace(tzinfo=UTC)
            samples = []
            try:
                for hour in HOURS:
                    url = currents.model_url(date, hour_of_cycle, hour)
                    path = Path(folder) / f'wind-f{hour:03d}.nc'
                    print(f'Downloading {url}', flush=True)
                    size = currents.download(url, path)
                    print(f'  {size / 1048576:.1f} MiB', flush=True)
                    samples.append(wind.extract(str(path), url, cyc))
                    path.unlink(missing_ok=True)
                cycle = validate_timeline(samples)
                if cycle != cyc:
                    raise ValueError('Unexpected cycle time from extracted files')
                result = sample_to_report(samples, cycle, HOURS)
                Path(output).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
                print('SUCCESS: 9 valid wind snapshots, 24-hour span. Output:', output)
                for row in result['snapshots']:
                    print(row['validUTC'], ' | '.join(
                        f"{name}: {values['speedMph']:.1f} mph from {values['fromDegrees']} deg"
                        for name, values in row['locations'].items()))
                return
            except Exception as exc:
                error = f'{date} t{hour_of_cycle:02d}z: {exc}'
                print('Candidate failed:', error, file=sys.stderr, flush=True)
                errors.append(error)
                for path in Path(folder).glob('wind-f*.nc'):
                    path.unlink(missing_ok=True)
    raise RuntimeError('No complete NOAA wind timeline available. Recent attempts: ' + '; '.join(errors[-4:]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--out', default='wind-timeline-pilot.json')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        run(args.out)


if __name__ == '__main__':
    main()
