#!/usr/bin/env python3
"""Create a Bellingham Bay FVCOM JSON subset from NOAA SSCOFS NetCDF fields.

Manual trial (one NOAA forecast snapshot, no GitHub commit):
  python scripts/update_sscofs.py --hours 3

Test using a previously requested NOAA regional NetCDF subset:
  python scripts/update_sscofs.py --sample /path/to/subset.nc

Output: currents-live-data.json (V4-compatible structure).
Requires requests, numpy and netCDF4 (GitHub Actions installs them).
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
import requests

try:
    import netCDF4
except ImportError:
    netCDF4 = None
    import h5py

# Expanded bounding area for the Bellingham Bay prototype.
# Triangles intersecting the box are excluded unless ALL vertices are inside.
SOUTH, NORTH, WEST, EAST = 48.48, 48.87, -122.83, -122.28
BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/nosofs/prod'
CYCLES = (21, 15, 9, 3)


def nc_open(path):
    if netCDF4 is not None:
        return netCDF4.Dataset(str(path))
    return h5py.File(str(path), 'r')


def valid_node_arrays(ds):
    lat = np.asarray(ds.variables['lat'][:] if hasattr(ds, 'variables') else ds['lat'][:], dtype=float).ravel()
    lon = np.asarray(ds.variables['lon'][:] if hasattr(ds, 'variables') else ds['lon'][:], dtype=float).ravel()
    lon = np.where(lon > 180, lon - 360, lon)
    return lat, lon


def read_mesh(ds):
    lat, lon = valid_node_arrays(ds)
    nv = np.asarray(ds.variables['nv'][:] if hasattr(ds, 'variables') else ds['nv'][:], dtype=int)
    if nv.shape[0] == 3:
        nv = nv.T
    if nv.ndim != 2 or nv.shape[1] != 3:
        raise ValueError(f'Unexpected connectivity shape: {nv.shape}')
    if nv.min() == 1:
        nv -= 1
    if nv.min() < 0 or nv.max() >= len(lat):
        raise ValueError('Mesh connectivity outside node array')
    inside = (lat >= SOUTH) & (lat <= NORTH) & (lon >= WEST) & (lon <= EAST)
    selected = np.flatnonzero(inside[nv].all(axis=1))
    if len(selected) < 100:
        raise ValueError(f'Too few Bellingham water cells: {len(selected)}')
    node_ids = np.unique(nv[selected].ravel())
    indices = np.full(len(lat), -1, dtype=np.int32)
    indices[node_ids] = np.arange(len(node_ids))
    result_nodes = np.round(np.column_stack([lat[node_ids], lon[node_ids]]), 6)
    result_tris = indices[nv[selected]]
    return selected, node_ids, result_nodes, result_tris


def field(ds, name):
    return ds.variables[name] if hasattr(ds, 'variables') else ds[name]


def get_3d_surface_index(ds):
    try:
        sigma = np.asarray(field(ds, 'siglay')[:], dtype=float)
        # FVCOM siglay: surface is closest to zero, regardless of dimension order.
        sigma = sigma.mean(axis=1) if sigma.ndim == 2 and sigma.shape[0] < sigma.shape[1] else (sigma.mean(axis=0) if sigma.ndim == 2 else sigma)
        return int(np.nanargmax(sigma))
    except (KeyError, ValueError, IndexError):
        return 0


def read_selected(ds, name, ids):
    v = field(ds, name)
    # SSCOFS fields usually have dims (time, siglay, nele) for 3D velocity.
    # Some subset files store just (time, nele).
    shape = v.shape
    if len(shape) == 3:
        layer = min(get_3d_surface_index(ds), shape[1] - 1)
        out = np.asarray(v[0, layer, ids], dtype=float)
    elif len(shape) == 2:
        out = np.asarray(v[0, ids], dtype=float)
    else:
        raise ValueError(f'Unexpected {name} dimensions: {shape}')
    return np.ma.filled(out, np.nan) if np.ma.isMaskedArray(out) else out


def parse_time(ds, default=None, index=0):
    try:
        times = field(ds, 'Times')
        row = np.asarray(times[index]).ravel()
        text = ''.join(x.decode('ascii', errors='ignore') if isinstance(x, bytes) else str(x) for x in row).strip().replace(' ', 'T')
        result = dt.datetime.fromisoformat(text[:19]).replace(tzinfo=dt.timezone.utc)
        return result.isoformat().replace('+00:00', 'Z')
    except Exception:
        try:
            x = field(ds, 'time')
            if netCDF4 is not None:
                t = netCDF4.num2date(x[index], x.units, only_use_cftime_datetimes=False)
                return dt.datetime(t.year, t.month, t.day, t.hour, t.minute, t.second, tzinfo=dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        except Exception:
            pass
    if default is not None:
        return default.isoformat().replace('+00:00', 'Z')
    raise ValueError('Could not determine model valid time')


def read_wet(ds, ids):
    try:
        w = field(ds, 'wet_cells')
        if len(w.shape) == 2:
            return np.asarray(w[0, ids]) != 0
        if len(w.shape) == 1:
            return np.asarray(w[ids]) != 0
    except (KeyError, IndexError):
        pass
    return np.ones(len(ids), dtype=bool)


def vectors_at_nodes(mesh, u, v, wet):
    ids, node_ids, nodes, tris = mesh
    good = wet & np.isfinite(u) & np.isfinite(v) & (np.abs(u) < 10) & (np.abs(v) < 10)
    accum_u = np.zeros(len(nodes)); accum_v = np.zeros(len(nodes)); counts = np.zeros(len(nodes))
    for j in range(3):
        np.add.at(accum_u, tris[good, j], u[good]); np.add.at(accum_v, tris[good, j], v[good]); np.add.at(counts, tris[good, j], 1)
    if good.sum() < 100:
        raise ValueError('Too few valid wet current cells')
    counts = np.maximum(counts, 1)
    return np.rint(np.column_stack([accum_u / counts, accum_v / counts]) * 1000).astype(int).tolist()


def convert_files(files, source, output):
    mesh = None
    times = []
    vectors = []
    for path, default_time in files:
        print(f'Processing: {path}', flush=True)
        with nc_open(path) as ds:
            selection = read_mesh(ds)
            if mesh is None:
                mesh = selection
            else:
                if not np.array_equal(mesh[0], selection[0]) or not np.allclose(mesh[2], selection[2]):
                    raise ValueError('NOAA mesh changed across files; stopping rather than mixing grids')
            # A subsetting-tool sample can have multiple time records, whereas
            # NOAA fields files normally have one record each.
            ntime = field(ds, 'u').shape[0]
            for idx in range(ntime):
                # Remote NOAA fields normally use idx=0. For the sample, use all records.
                if idx == 0:
                    u = read_selected(ds, 'u', mesh[0]); v = read_selected(ds, 'v', mesh[0]); wet = read_wet(ds, mesh[0])
                else:
                    vv = field(ds, 'u'); ww = field(ds, 'v')
                    u = np.asarray(vv[idx, mesh[0]], dtype=float)
                    v = np.asarray(ww[idx, mesh[0]], dtype=float)
                    wet = np.asarray(field(ds, 'wet_cells')[idx, mesh[0]]) != 0 if 'wet_cells' in (ds.variables if hasattr(ds, 'variables') else ds) else np.ones(len(mesh[0]), bool)
                vectors.append(vectors_at_nodes(mesh, u, v, wet))
                times.append(parse_time(ds, default_time, idx))
    order = sorted(range(len(times)), key=lambda j: times[j])
    if len(set(times)) != len(times):
        raise ValueError('Duplicate model timestamps')
    payload = {
        'metadata': {
            'model': 'NOAA SSCOFS FVCOM',
            'kind': source,
            'velocityUnits': 'millimeters per second',
            'currentDatum': 'surface',
            'note': 'NOAA model guidance, not direct observations. Do not use for navigation.',
            'cells': len(mesh[3]),
            'nodes': len(mesh[2]),
            'updatedUTC': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'bounds': [SOUTH, WEST, NORTH, EAST],
        },
        'times': [times[j] for j in order],
        'nodes': mesh[2].tolist(),
        'triangles': mesh[3].tolist(),
        'vectors': [vectors[j] for j in order],
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(',', ':')))
    print(f'Wrote {output}: {output.stat().st_size:,} bytes; {len(payload["times"])} snapshots; '
          f'{len(payload["triangles"])} triangles; {len(payload["nodes"])} nodes', flush=True)


def candidate_runs(now):
    # Try recent cycles in reverse chronological order, including previous UTC day.
    dates = [(now - dt.timedelta(days=d)).strftime('%Y%m%d') for d in (0, 1)]
    for date in dates:
        for cycle in CYCLES:
            cycletime = dt.datetime.strptime(date + f'{cycle:02d}', '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            if cycletime + dt.timedelta(minutes=100) > now:
                continue
            yield date, cycle


def model_url(date, cycle, forecast_hour):
    return (f'{BASE}/sscofs.{date}/sscofs.t{cycle:02d}z.{date}'
            f'.fields.f{forecast_hour:03d}.nc')


def download(url, dest):
    with requests.get(url, stream=True, timeout=(20, 180), headers={'User-Agent': 'bellingham-conditions-research/0.1'}) as r:
        r.raise_for_status()
        maximum = 300 * 1024 * 1024
        size = 0
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError('Unexpected NOAA file size (>300 MB)')
                    f.write(chunk)
        if size < 1024 * 1024:
            raise ValueError(f'Suspiciously small NOAA response ({size} bytes)')
    return size


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sample', help='Local NOAA subset for offline conversion test')
    p.add_argument('--hours', default='3', help='Comma-separated forecast hours, e.g. 3 or 3,6,9 (one NOAA file per hour)')
    p.add_argument('--output', default='currents-live-data.json')
    args = p.parse_args()
    if args.sample:
        convert_files([(Path(args.sample), None)], 'historical modeled nowcast sample', args.output)
        return
    hours = [int(x) for x in args.hours.split(',')]
    if not hours or len(hours) > 5 or any(h < 0 or h > 72 for h in hours):
        raise SystemExit('Choose 1-5 forecast hours from 0 to 72')
    now = dt.datetime.now(dt.timezone.utc)
    failures = []
    with tempfile.TemporaryDirectory() as d:
        for date, cycle in candidate_runs(now):
            downloaded = []
            try:
                for hour in hours:
                    url = model_url(date, cycle, hour)
                    dest = Path(d) / f'fields-{hour:03d}.nc'
                    print(f'Downloading {url}', flush=True)
                    size = download(url, dest)
                    print(f'  Received {size / 1048576:.1f} MiB', flush=True)
                    nominal_time = dt.datetime.strptime(date + f'{cycle:02d}', '%Y%m%d%H').replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=hour)
                    downloaded.append((dest, nominal_time))
                convert_files(downloaded, 'operational modeled forecast', args.output)
                return
            except Exception as e:
                failures.append(f'{date} t{cycle:02d}z: {e}')
                print(f'Candidate failed: {failures[-1]}', file=sys.stderr, flush=True)
                # Clean files before trying older cycle.
                for child in Path(d).glob('*.nc'):
                    child.unlink(missing_ok=True)
        raise SystemExit('No model cycle could be processed. Last errors:\n' + '\n'.join(failures[-4:]))


if __name__ == '__main__':
    main()
