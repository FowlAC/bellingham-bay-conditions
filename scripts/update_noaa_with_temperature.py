#!/usr/bin/env python3
"""Reuse current-map NOAA downloads to publish a separate surface-temp candidate.

This is a thin adapter around the existing update_sscofs.py. That existing code
remains unchanged. The adapter inspects one of its already-downloaded NetCDF
files before the temporary download directory is removed.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import netCDF4
import update_sscofs as currents
from test_sscofs_temperature import inspect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', default='3,6,9,12,15,18,21,24,27')
    parser.add_argument('--output', default='currents-candidate.json')
    parser.add_argument('--temp-output', default='water-temp-candidate.json')
    args = parser.parse_args()
    hours = [int(v) for v in args.hours.split(',')]
    if not hours or len(set(hours)) != len(hours) or len(hours) > 9:
        raise SystemExit('Invalid forecast hours')

    # Ensure a failed temperature extraction cannot leave a previous candidate.
    Path(args.temp_output).unlink(missing_ok=True)
    original = currents.convert_files

    def convert_with_temperature(files, source, output):
        # The live current pipeline is always run and validated as before.
        original(files, source, output)
        if source != 'operational modeled forecast':
            print('Temperature extraction skipped for nonoperational sample')
            return
        try:
            now = dt.datetime.now(dt.timezone.utc)
            # Prefer snapshot nearest current time, not the furthest forecast.
            paired = list(zip(files, hours))
            (path, valid_time), hour = min(
                paired,
                key=lambda entry: abs((entry[0][1] - now).total_seconds()),
            )
            cycle = valid_time - dt.timedelta(hours=hour)
            date = cycle.strftime('%Y%m%d')
            url = currents.model_url(date, cycle.hour, hour)
            with netCDF4.Dataset(str(path)) as ds:
                result = inspect(ds, date, cycle.hour, valid_time, url)
            if abs((dt.datetime.fromisoformat(result['modelValidUTC'].replace('Z', '+00:00')) - valid_time).total_seconds()) > 3600:
                raise ValueError('Temperature model-valid timestamp disagrees with currents file')
            result['status'] = 'candidate'
            result['generatedUTC'] = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
            result['note'] = 'NOAA modeled surface-water temperature, not a measured observation. Not for navigation.'
            dest = Path(args.temp_output)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            print(f'Temperature candidate: {result["temperatureF"]} F, valid {result["modelValidUTC"]}; file {dest}')
        except Exception as exc:
            Path(args.temp_output).unlink(missing_ok=True)
            print(f'Temperature candidate unavailable (currents remain valid): {exc}', file=sys.stderr)

    currents.convert_files = convert_with_temperature
    sys.argv = [sys.argv[0], '--hours', args.hours, '--output', args.output]
    currents.main()


if __name__ == '__main__':
    main()
