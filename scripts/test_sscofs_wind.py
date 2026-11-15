#!/usr/bin/env python3
"""Manual-only NOAA SSCOFS wind forcing extraction pilot.

Independent test: does not change or publish the operational currents.
Requires numpy, netCDF4, requests. Run with --self-test for offline QA.
"""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import tempfile

import numpy as np
import requests

UTC = dt.timezone.utc
MPH_PER_MPS = 2.2369362921
# Sampling locations approximate marine locations; model nodes are selected nearby.
SITES = [
    ("Central Bellingham Bay", 48.690, -122.550),
    ("Post Point / open bay", 48.675, -122.550),
    ("North bay / near Locust Beach", 48.774, -122.570),
]
MAX_DOWNLOAD_BYTES = 650 * 1024 * 1024


def wind_from_uv(u, v):
    """Meteorological wind direction (FROM), degrees true, and speed in mph."""
    return (math.hypot(u, v) * MPH_PER_MPS,
            (math.degrees(math.atan2(-u, -v)) + 360) % 360)


def self_test():
    examples = [(0, -10, 0), (10, 0, 270), (0, 10, 180), (-10, 0, 90)]
    for u, v, expected in examples:
        speed, angle = wind_from_uv(u, v)
        assert abs(speed - 22.36936) < .001
        assert abs(angle - expected) < .001
    print("PASS: wind vector conversion, direction convention, mph")


def candidates():
    now = dt.datetime.now(UTC)
    for delta in range(0, 3):
        day = (now - dt.timedelta(days=delta)).strftime('%Y%m%d')
        for cycle in (21, 15, 9, 3):
            cycle_time = dt.datetime.strptime(day + f'{cycle:02d}', '%Y%m%d%H').replace(tzinfo=UTC)
            if cycle_time > now - dt.timedelta(hours=3):
                continue
            # f003: 3-hour forecast, same file family as successful temp pilot
            yield (f'https://nomads.ncep.noaa.gov/pub/data/nccf/com/nosofs/prod/'
                   f'sscofs.{day}/sscofs.t{cycle:02d}z.{day}.fields.f003.nc', cycle_time)


def download_one(url, path):
    with requests.get(url, stream=True, timeout=(20, 150), headers={'User-Agent': 'BellinghamBayWindPilot/1.0 (research; NOAA public data)'}) as r:
        if r.status_code != 200:
            raise RuntimeError(f'HTTP {r.status_code}')
        total = 0
        with open(path, 'wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError('NetCDF file exceeds pilot size limit')
                    f.write(chunk)
    if total < 10000:
        raise RuntimeError('Download unexpectedly small')
    return total


def datetime_from_dataset(ds):
    from netCDF4 import num2date
    if 'time' in ds.variables:
        t = ds.variables['time']
        if getattr(t, 'units', None):
            try:
                x = num2date(t[0], t.units, calendar=getattr(t, 'calendar', 'standard'),
                             only_use_cftime_datetimes=False)
                return f'{int(x.year):04d}-{int(x.month):02d}-{int(x.day):02d}T{int(x.hour):02d}:{int(x.minute):02d}:{int(x.second):02d}Z'
            except Exception:
                pass
    return None


def coordinates(ds, key):
    if key not in ds.variables:
        raise KeyError(f'Missing {key}; found {list(ds.variables.keys())[:35]}')
    return np.ma.filled(ds.variables[key][:], np.nan).astype(float).ravel()


def values(ds, name):
    if name not in ds.variables:
        raise KeyError(f'Missing {name}; found {list(ds.variables.keys())[:45]}')
    var = ds.variables[name]
    # Common SSCOFS layout: (time, nele); handle optional extra dimensions explicitly.
    if len(var.dimensions) == 2:
        arr = var[0, :]
    elif len(var.dimensions) == 1:
        arr = var[:]
    else:
        raise ValueError(f'Unexpected {name} dimensions: {var.dimensions}')
    return np.ma.filled(arr, np.nan).astype(float).ravel(), list(var.dimensions), getattr(var, 'units', '')


def extract(path, url, cycle):
    from netCDF4 import Dataset
    with Dataset(path, 'r') as ds:
        u, udims, units_u = values(ds, 'uwind_speed')
        v, vdims, units_v = values(ds, 'vwind_speed')
        lat = coordinates(ds, 'latc')
        lon = coordinates(ds, 'lonc')
        if len(u) != len(v) or len(u) != len(lat) or len(u) != len(lon):
            raise ValueError(f'Wind/coordinate size mismatch: u={len(u)}, v={len(v)}, lat={len(lat)}, lon={len(lon)}')
        if not ('m' in units_u.lower() and 's' in units_u.lower() and
                'm' in units_v.lower() and 's' in units_v.lower()):
            raise ValueError(f'Unexpected wind units: {units_u!r}, {units_v!r}')
        lon = ((lon + 180) % 360) - 180
        mask = np.isfinite(u) & np.isfinite(v) & np.isfinite(lat) & np.isfinite(lon)
        mask &= (abs(u) <= 80) & (abs(v) <= 80)
        if not mask.any():
            raise ValueError('No valid wind vectors')
        records = []
        for name, target_lat, target_lon in SITES:
            # Haversine approximation for short distances; choose marine mesh faces in 2.5 km radius.
            north_km = (lat - target_lat) * 111.2
            east_km = (lon - target_lon) * 111.2 * math.cos(math.radians(target_lat))
            dist = np.hypot(north_km, east_km)
            chosen = np.flatnonzero(mask & (dist <= 2.5))
            if len(chosen) < 3:
                records.append({'location': name, 'available': False, 'note': 'Too few valid mesh cells within 2.5 km'})
                continue
            # Limit to nearest 60 faces, reducing overweighting from dense nearshore mesh.
            chosen = chosen[np.argsort(dist[chosen])[:60]]
            ue = float(np.median(u[chosen]))
            vn = float(np.median(v[chosen]))
            speed, direction = wind_from_uv(ue, vn)
            speeds = np.hypot(u[chosen], v[chosen]) * MPH_PER_MPS
            records.append({
                'location': name, 'available': True,
                'target': {'lat': target_lat, 'lon': target_lon},
                'nearestMeshCellKm': round(float(np.min(dist[chosen])), 3),
                'sampleCells': int(len(chosen)),
                'windSpeedMph': round(speed, 1), 'windFromDegrees': round(direction),
                'speedRangeMph': [round(float(np.min(speeds)), 1), round(float(np.max(speeds)), 1)],
                'meanEastwardMps': round(ue, 3), 'meanNorthwardMps': round(vn, 3)
            })
        model_valid = datetime_from_dataset(ds)
    # f003 means 3-hour forecast; fallback when NetCDF time metadata can't be decoded.
    if not model_valid:
        model_valid = (cycle + dt.timedelta(hours=3)).isoformat().replace('+00:00', 'Z')
    return {
        'status': 'pilot_only_not_published', 'dataType': 'sscofs_wind_forcing_forecast',
        'source': 'NOAA SSCOFS FVCOM model wind forcing', 'sourceFile': url,
        'modelCycleUTC': cycle.isoformat().replace('+00:00', 'Z'),
        'modelValidUTC': model_valid,
        'windUnits': 'mph', 'windDirectionConvention': 'meteorological FROM degrees true',
        'fieldVariables': ['uwind_speed', 'vwind_speed'],
        'fieldDimensions': udims, 'nativeUnits': units_u,
        'locations': records,
        'note': ('This is model wind forcing, NOT an observed wind reading or independently '
                 'validated bay wind forecast. It does not include gusts or wave height. '
                 'Use only for research until independently compared against local observations.')
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--local-netcdf', help='Optional local SSCOFS fields NetCDF for test')
    p.add_argument('--out', default='wind-pilot.json')
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    result = None
    if args.local_netcdf:
        path = pathlib.Path(args.local_netcdf)
        result = extract(str(path), path.as_uri(), dt.datetime.now(UTC))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            for url, cycle in candidates():
                path = pathlib.Path(tmp) / 'sscofs-wind.nc'
                try:
                    print(f'Trying {url}', flush=True)
                    size = download_one(url, path)
                    print(f'Downloaded {size / 1e6:.1f} MB', flush=True)
                    result = extract(str(path), url, cycle)
                    break
                except (requests.RequestException, RuntimeError, OSError, ValueError, KeyError) as e:
                    print(f'Candidate unavailable/unusable: {str(e)[:220]}', flush=True)
                    if path.exists():
                        path.unlink()
        if result is None:
            raise RuntimeError('No usable NOAA SSCOFS forecast file found; inspect URLs in logs')
    pathlib.Path(args.out).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('Pilot results:', json.dumps(result, indent=2))
    print('Saved', args.out)


if __name__ == '__main__':
    main()
