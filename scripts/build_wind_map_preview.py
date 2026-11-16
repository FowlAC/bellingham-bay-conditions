#!/usr/bin/env python3
"""SSCOFS mesh wind map: 9 three-hourly snapshots.

Generates a candidate only; separate validation and publication are required.
Requires numpy, requests, netCDF4; imports existing scripts/update_sscofs.py.
"""
import argparse
import datetime as dt
import json
import math
import sys
import tempfile
from pathlib import Path
import numpy as np
import update_sscofs as noaa

UTC = dt.timezone.utc
HOURS = tuple(range(3, 28, 3))
# Marine forecast map focused on Bellingham Bay, Post Point and northern shore.
SOUTH, NORTH, WEST, EAST = 48.59, 48.84, -122.75, -122.43
# At most one FVCOM cell from each ~1-km map bin, keeps JSON and rendering small.
LAT_STEP, LON_STEP = .009, .013

def pick_cells(ds):
    lat = np.ma.filled(ds.variables['latc'][:], np.nan).astype(float).ravel()
    lon = np.ma.filled(ds.variables['lonc'][:], np.nan).astype(float).ravel()
    lon = ((lon + 180) % 360) - 180
    eligible = np.where(np.isfinite(lat) & np.isfinite(lon) &
        (lat >= SOUTH) & (lat <= NORTH) & (lon >= WEST) & (lon <= EAST))[0]
    bins = {}
    for i in eligible:
        row = int((lat[i]-SOUTH)/LAT_STEP)
        col = int((lon[i]-WEST)/LON_STEP)
        center_lat = SOUTH+(row+.5)*LAT_STEP
        center_lon = WEST+(col+.5)*LON_STEP
        d2 = ((lat[i]-center_lat)*111.2)**2 + ((lon[i]-center_lon)*73.5)**2
        key = (row,col)
        if key not in bins or d2 < bins[key][0]:
            bins[key] = (d2,int(i))
    indices = np.array(sorted(v[1] for v in bins.values()),dtype=np.int64)
    if len(indices) < 80 or len(indices) > 1800:
        raise ValueError(f'Unexpected number of marine cells: {len(indices)}')
    return indices, np.column_stack((lat[indices],lon[indices]))

def parse_time(ds, cycle, hour):
    result = noaa.parse_time(ds, cycle + dt.timedelta(hours=hour))
    value = dt.datetime.fromisoformat(result.replace('Z','+00:00'))
    expected = cycle + dt.timedelta(hours=hour)
    if abs((value-expected).total_seconds()) > 900:
        raise ValueError(f'Model timestamp {result} differs from expected {expected}')
    return value.isoformat().replace('+00:00','Z')

def read_one(path, ids, expected_coordinates, cycle, hour):
    from netCDF4 import Dataset
    with Dataset(str(path)) as ds:
        if 'uwind_speed' not in ds.variables or 'vwind_speed' not in ds.variables:
            raise ValueError('NOAA wind variables missing')
        lat = np.ma.filled(ds.variables['latc'][ids], np.nan).astype(float)
        lon = np.ma.filled(ds.variables['lonc'][ids], np.nan).astype(float)
        lon = ((lon+180)%360)-180
        if not np.allclose(np.column_stack((lat,lon)), expected_coordinates, atol=1e-6):
            raise ValueError('Wind mesh changed between forecast files')
        uvar, vvar = ds.variables['uwind_speed'], ds.variables['vwind_speed']
        if uvar.ndim != 2 or vvar.ndim != 2:
            raise ValueError('Expected (time,nele) wind fields')
        if not all('m' in getattr(x,'units','').lower() and 's' in getattr(x,'units','').lower() for x in (uvar,vvar)):
            raise ValueError('Unexpected NOAA wind units')
        u = np.ma.filled(uvar[0,ids],np.nan).astype(float)
        v = np.ma.filled(vvar[0,ids],np.nan).astype(float)
        ok = np.isfinite(u) & np.isfinite(v) & (np.hypot(u,v) <= 45)
        if ok.sum() < 80 or ok.mean() < .94:
            raise ValueError('Too few valid wind cells')
        # Integer decimeters per second: 0.1 m/s precision.
        values = [[int(round(a*10)),int(round(b*10))] if good else None for a,b,good in zip(u,v,ok)]
        return parse_time(ds,cycle,hour),values

def make_payload(cycle, timestamps, points, all_vectors):
    if len(timestamps)!=9 or len(set(timestamps))!=9 or len(all_vectors)!=9:
        raise ValueError('Expected nine unique forecast snapshots')
    if any(len(v)!=len(points) for v in all_vectors):
        raise ValueError('Wind frame/mesh count mismatch')
    return {'metadata': {
        'source':'NOAA SSCOFS FVCOM atmospheric wind forcing',
        'type':'modeled_wind_not_observed',
        'modelCycleUTC': cycle.isoformat().replace('+00:00','Z'),
        'createdUTC':dt.datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00','Z'),
        'forecastHours':list(HOURS), 'vectorUnits':'decimeters_per_second',
        'vectorConvention':'eastward,northward TO; display direction FROM true north',
        'bounds':[SOUTH,WEST,NORTH,EAST], 'cellCount':len(points),
        'note':'Experimental NOAA wind forcing, not an independently validated local wind forecast. No gust/wave predictions. Not for navigation or safety.'
        }, 'times':timestamps,'points':np.round(points,5).tolist(),'vectors':all_vectors}

def run(out):
    now=dt.datetime.now(UTC)
    failures=[]
    with tempfile.TemporaryDirectory() as tmp:
        for date,cycle_hr in noaa.candidate_runs(now):
            cycle=dt.datetime.strptime(date+f'{cycle_hr:02d}','%Y%m%d%H').replace(tzinfo=UTC)
            ids=coords=None
            times=[]; frames=[]
            try:
                for hr in HOURS:
                    url=noaa.model_url(date,cycle_hr,hr)
                    filename=Path(tmp)/f'wind-preview-{hr:03d}.nc'
                    print('Fetching',url,flush=True)
                    noaa.download(url,filename)
                    if ids is None:
                        from netCDF4 import Dataset
                        with Dataset(str(filename)) as ds:
                            ids,coords=pick_cells(ds)
                        print('Selected marine cells',len(ids),flush=True)
                    timestamp,values=read_one(filename,ids,coords,cycle,hr)
                    times.append(timestamp);frames.append(values)
                    filename.unlink(missing_ok=True)
                payload=make_payload(cycle,times,coords,frames)
                Path(out).write_text(json.dumps(payload,separators=(',',':'))+'\n',encoding='utf8')
                print('SUCCESS',out,Path(out).stat().st_size,'bytes',flush=True)
                return
            except Exception as exc:
                msg=f'{date} t{cycle_hr:02d}z: {exc}'
                print('Candidate failed:',msg,file=sys.stderr,flush=True)
                failures.append(msg)
                for f in Path(tmp).glob('wind-preview-*.nc'): f.unlink(missing_ok=True)
    raise RuntimeError('No complete model cycle: '+'; '.join(failures[-3:]))

def selftest():
    points=np.array([[48.7,-122.55],[48.75,-122.53]])
    cycle=dt.datetime(2026,11,16,15,tzinfo=UTC)
    times=[(cycle+dt.timedelta(hours=h)).isoformat().replace('+00:00','Z') for h in HOURS]
    frames=[[[10,0],[0,10]] for _ in HOURS]
    p=make_payload(cycle,times,points,frames)
    assert len(p['times'])==9 and len(p['points'])==2
    assert p['vectors'][0][0]==[10,0]
    assert abs(math.hypot(*p['vectors'][0][0])/10*2.23694-2.23694)<.001
    print('PASS: 9 snapshots, mesh units, output schema')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--out',default='wind-map-preview-data.json')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    selftest() if a.self_test else run(a.out)
