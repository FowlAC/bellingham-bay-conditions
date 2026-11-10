#!/usr/bin/env python3
"""Validate NOAA Bellingham forecast JSON before allowing an automatic publication."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path

UTC = dt.timezone.utc


def parse_utc(value):
    timestamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError(f'Timestamp must have a UTC offset: {value}')
    return timestamp.astimezone(UTC)


def load(path):
    with open(path, encoding='utf-8') as file:
        return json.load(file)


def validate(candidate, now):
    meta = candidate['metadata']
    if meta.get('model') != 'NOAA SSCOFS FVCOM' or meta.get('kind') != 'operational modeled forecast':
        raise ValueError('Unexpected model name or dataset type')
    if meta.get('velocityUnits') != 'millimeters per second':
        raise ValueError('Unexpected velocity units')
    nodes = candidate['nodes']
    triangles = candidate['triangles']
    times = candidate['times']
    vectors = candidate['vectors']
    if not (1000 <= len(nodes) <= 100000 and 1000 <= len(triangles) <= 300000):
        raise ValueError('Unexpected water mesh dimensions')
    if not (len(times) == len(vectors) == 9):
        raise ValueError('Require exactly nine forecast snapshots for the 24-hour timeline')
    if meta.get('nodes') != len(nodes) or meta.get('cells') != len(triangles):
        raise ValueError('Mesh metadata does not match actual dimensions')
    if not all(isinstance(pt, list) and len(pt) == 2 and
               all(isinstance(v, (int, float)) and math.isfinite(v) for v in pt) and
               48.4 <= pt[0] <= 49 and -123 <= pt[1] <= -122
               for pt in nodes):
        raise ValueError('Invalid or geographically unexpected coordinates')
    if not all(isinstance(tri, list) and len(tri) == 3 and
               all(isinstance(index, int) and 0 <= index < len(nodes) for index in tri)
               for tri in triangles):
        raise ValueError('Invalid triangle connectivity')
    ts = [parse_utc(s) for s in times]
    if ts != sorted(set(ts)):
        raise ValueError('Forecast times are not unique and strictly increasing')
    if any((b - a) != dt.timedelta(hours=3) for a, b in zip(ts, ts[1:])):
        raise ValueError('Forecasts are not three hours apart')
    if ts[0] < now - dt.timedelta(hours=18) or ts[0] > now + dt.timedelta(hours=18):
        raise ValueError('First forecast is too old or implausibly far ahead')
    for snapshot in vectors:
        if len(snapshot) != len(nodes):
            raise ValueError('Velocity snapshot size does not match the node count')
        if not all(isinstance(p, list) and len(p) == 2 and
                   all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= 10000 for v in p)
                   for p in snapshot):
            raise ValueError('Invalid or extreme current vector')
    print(f'Validated {len(ts)} snapshots, {len(nodes)} nodes, {len(triangles)} triangles; '
          f'valid from {times[0]} through {times[-1]}')
    return ts


def payload_signature(data):
    # updatedUTC is set by the converter every run, even when NOAA data is identical.
    # Do not redeploy solely because the converter ran again.
    comparable = {k: v for k, v in data.items() if k != 'metadata'}
    comparable['metadata'] = {k: v for k, v in data['metadata'].items() if k != 'updatedUTC'}
    return comparable


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--candidate', required=True)
    p.add_argument('--existing', required=True)
    p.add_argument('--github-output')
    args = p.parse_args()
    candidate = load(args.candidate)
    now = dt.datetime.now(UTC)
    candidate_times = validate(candidate, now)
    publish = True
    existing_path = Path(args.existing)
    if existing_path.is_file():
        existing = load(existing_path)
        try:
            existing_last = parse_utc(existing['times'][-1])
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise ValueError('Published file invalid; refusing to overwrite automatically') from exc
        if candidate_times[-1] < existing_last:
            raise ValueError('NOAA candidate is older than the published dataset')
        if payload_signature(candidate) == payload_signature(existing):
            publish = False
            print('No changes in NOAA timestamps, mesh or vectors')
    print('Publish:', str(publish).lower())
    if args.github_output:
        with open(args.github_output, 'a', encoding='utf-8') as file:
            file.write(f'publish={str(publish).lower()}\n')


if __name__ == '__main__':
    main()
