#!/usr/bin/env python3
"""Validate SSCOFS wind forecast candidate before publishing to Cloudflare Pages.

Exit 0 and write publish=true/false to GITHUB_OUTPUT for valid candidates.
Exit nonzero for corrupt, expired or implausible data (never publish).
"""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path

UTC = dt.timezone.utc
HOURS = list(range(3, 28, 3))
BOUNDS = [48.59, -122.75, 48.84, -122.43]

def parse_utc(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('UTC timestamp must be ISO 8601 with Z suffix')
    date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None: raise ValueError('Timestamp has no timezone')
    return date.astimezone(UTC)

def validate(data, now):
    if not isinstance(data, dict): raise ValueError('Root must be object')
    meta = data.get('metadata')
    if not isinstance(meta, dict): raise ValueError('Missing metadata')
    if meta.get('type') != 'modeled_wind_not_observed': raise ValueError('Wrong data type')
    if meta.get('vectorUnits') != 'decimeters_per_second': raise ValueError('Wrong vector units')
    if meta.get('forecastHours') != HOURS: raise ValueError('Unexpected forecast hours')
    if meta.get('bounds') != BOUNDS: raise ValueError('Unexpected map extent')
    cycle = parse_utc(meta.get('modelCycleUTC'))
    if cycle > now + dt.timedelta(hours=1): raise ValueError('Model cycle lies in future')
    if now - cycle > dt.timedelta(hours=22): raise ValueError('Model cycle is stale (>22h)')
    points, frames, times = data.get('points'), data.get('vectors'), data.get('times')
    if not isinstance(points, list) or not 150 <= len(points) <= 1600: raise ValueError('Unexpected cell count')
    if meta.get('cellCount') != len(points): raise ValueError('Cell count mismatch')
    if not isinstance(times, list) or len(times) != 9: raise ValueError('Expected nine times')
    if not isinstance(frames, list) or len(frames) != 9: raise ValueError('Expected nine frames')
    for i, t in enumerate(times):
        if parse_utc(t) != cycle + dt.timedelta(hours=HOURS[i]):
            raise ValueError(f'Frame {i}: forecast timestamp wrong')
    if parse_utc(times[-1]) <= now: raise ValueError('Forecast already ended')
    for i, p in enumerate(points):
        if not isinstance(p, list) or len(p) != 2: raise ValueError(f'Cell {i}: invalid coordinates')
        lat, lon = p
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in p) or not (BOUNDS[0] <= lat <= BOUNDS[2] and BOUNDS[1] <= lon <= BOUNDS[3]):
            raise ValueError(f'Cell {i}: outside bounds')
    bad = 0
    for k, frame in enumerate(frames):
        if not isinstance(frame, list) or len(frame) != len(points): raise ValueError(f'Frame {k}: mesh length mismatch')
        for pair in frame:
            if pair is None:
                bad += 1; continue
            if not isinstance(pair, list) or len(pair)!=2 or not all(type(x) is int for x in pair):
                raise ValueError(f'Frame {k}: invalid vector')
            if math.hypot(*pair) > 450: raise ValueError(f'Frame {k}: implausible wind speed')
        if sum(p is not None for p in frame) < .94 * len(points):
            raise ValueError(f'Frame {k}: insufficient data coverage')
    return cycle, bad

def decide(candidate, existing, now):
    c, missing = validate(candidate, now)
    if existing is None: return True, f'first valid wind data: {c.isoformat()}, missing vectors {missing}'
    try:
        old_cycle = parse_utc(existing['metadata']['modelCycleUTC'])
        old_times = existing.get('times', [])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Published file has malformed metadata: {exc}')
    # Preserve the existing file unless the candidate has a strictly newer model cycle.
    if c <= old_cycle:
        return False, f'candidate cycle {c.isoformat()} is not newer than published {old_cycle.isoformat()}'
    if old_times and parse_utc(candidate['times'][-1]) <= parse_utc(old_times[-1]):
        return False, 'candidate forecast does not extend published horizon'
    return True, f'newer valid wind cycle: {c.isoformat()}, missing vectors {missing}'

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--candidate',required=True)
    p.add_argument('--existing',default='wind-map-preview-data.json')
    p.add_argument('--github-output',default=os.getenv('GITHUB_OUTPUT'))
    a=p.parse_args()
    candidate=json.loads(Path(a.candidate).read_text(encoding='utf8'))
    existing=json.loads(Path(a.existing).read_text(encoding='utf8')) if Path(a.existing).is_file() else None
    publish, message=decide(candidate,existing,dt.datetime.now(UTC))
    print(('PUBLISH: ' if publish else 'KEEP: ')+message)
    if a.github_output:
        with open(a.github_output,'a',encoding='utf8') as stream: stream.write('publish='+str(publish).lower()+'\n')

def selftest():
    now=dt.datetime(2026,11,16,21,tzinfo=UTC)
    cycle=now-dt.timedelta(hours=9)
    times=[(cycle+dt.timedelta(hours=h)).isoformat().replace('+00:00','Z') for h in HOURS]
    points=[[48.69+(i%10)*.001,-122.55+(i//10)*.001] for i in range(200)]
    m={'type':'modeled_wind_not_observed','vectorUnits':'decimeters_per_second','forecastHours':HOURS,'bounds':BOUNDS,'cellCount':len(points),'modelCycleUTC':cycle.isoformat().replace('+00:00','Z')}
    d={'metadata':m,'times':times,'points':points,'vectors':[[[10,20] for _ in points] for _ in HOURS]}
    assert decide(d,None,now)[0]
    assert not decide(d,d,now)[0]
    newer=json.loads(json.dumps(d)); newer['metadata']['modelCycleUTC']=(cycle+dt.timedelta(hours=6)).isoformat().replace('+00:00','Z'); newer['times']=[(cycle+dt.timedelta(hours=6+h)).isoformat().replace('+00:00','Z') for h in HOURS]
    assert decide(newer,d,now)[0]
    broken=json.loads(json.dumps(d)); broken['vectors'][2]=[]
    try: validate(broken,now); raise AssertionError('Expected rejection')
    except ValueError: pass
    oldnow=now+dt.timedelta(hours=28)
    try: validate(d,oldnow); raise AssertionError('Expected expiry rejection')
    except ValueError: pass
    print('PASS: complete/newer/unchanged/incomplete/stale checks')

if __name__=='__main__':
    import sys
    if '--self-test' in sys.argv: selftest()
    else: main()
