#!/usr/bin/env python3
"""Validate a modeled water-temperature candidate independently of currents."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path

UTC = dt.timezone.utc


def parse_time(value):
    d = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp without timezone')
    return d.astimezone(UTC)


def validate(data, now):
    if data.get('source') != 'NOAA SSCOFS FVCOM' or data.get('dataType') != 'modeled_surface_temperature':
        raise ValueError('Unexpected NOAA modeled temperature source/type')
    if data.get('status') != 'candidate':
        raise ValueError('Not a validated temperature candidate')
    if data.get('temperatureUnitsOriginal', '').lower() not in ('degrees_c', 'degrees_celsius', 'degree_celsius', 'celsius', 'degree c', 'degrees c', 'degc'):
        raise ValueError('Unverified Celsius units')
    c = float(data['temperatureC'])
    f = float(data['temperatureF'])
    if not (math.isfinite(c) and -2 < c < 36 and math.isfinite(f) and abs(f - (c * 1.8 + 32)) < .16):
        raise ValueError('Invalid temperature or Celsius/Fahrenheit conversion')
    center = data['sampleCenter']
    if abs(float(center['lat']) - 48.69) > .005 or abs(float(center['lon']) + 122.55) > .005:
        raise ValueError('Unexpected sample location')
    if float(data['sampleRadiusKm']) != 2.5 or int(data['validNodes']) < 3:
        raise ValueError('Invalid sample radius or too few valid model nodes')
    if int(data['validNodes']) < .70 * int(data['nodesInArea']):
        raise ValueError('Insufficient valid model nodes')
    if float(data['sigmaAtSelectedLayer']) < -.5 or float(data['sigmaAtSelectedLayer']) > .05:
        raise ValueError('Not a surface layer')
    valid = parse_time(data['modelValidUTC'])
    cycle = parse_time(data['modelCycleUTC'])
    generated = parse_time(data['generatedUTC'])
    if valid < now - dt.timedelta(hours=9) or valid > now + dt.timedelta(hours=9):
        raise ValueError('Model-valid time not close to current time')
    if cycle > now + dt.timedelta(minutes=10) or cycle < now - dt.timedelta(hours=18):
        raise ValueError('Model cycle too old or in the future')
    if abs((generated - now).total_seconds()) > 3600:
        raise ValueError('Candidate was not generated recently')
    if valid < cycle or valid > cycle + dt.timedelta(hours=72):
        raise ValueError('Invalid model forecast time relative to cycle')
    return valid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--existing', required=True)
    parser.add_argument('--github-output')
    args = parser.parse_args()
    candidate_path = Path(args.candidate)
    publish = False
    if candidate_path.is_file():
        with candidate_path.open(encoding='utf-8') as f:
            candidate = json.load(f)
        now = dt.datetime.now(UTC)
        valid = validate(candidate, now)
        existing_path = Path(args.existing)
        if existing_path.is_file():
            with existing_path.open(encoding='utf-8') as f:
                existing = json.load(f)
            old_valid = parse_time(existing['modelValidUTC'])
            if valid < old_valid:
                print('Temperature candidate older than published forecast; preserving existing file')
            elif valid == old_valid:
                print('Same model-valid time; no temperature redeploy')
            else:
                publish = True
        else:
            publish = True
        print(f'Validated NOAA temperature {candidate["temperatureF"]} F at {valid.isoformat()}')
    else:
        print('Temperature candidate absent; existing modeled temperature unchanged')
    if args.github_output:
        with open(args.github_output, 'a', encoding='utf-8') as f:
            f.write(f'publish={str(publish).lower()}\n')
    print('Publish temperature:', str(publish).lower())


if __name__ == '__main__':
    main()
