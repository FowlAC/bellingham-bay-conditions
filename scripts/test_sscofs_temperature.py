#!/usr/bin/env python3
"""Manual, nonpublishing NOAA SSCOFS surface-temperature pilot.

Downloads one forecast fields NetCDF file, checks the temperature variable's
actual dimensions, selects the surface sigma layer, and summarizes wet nodes
within 2.5 km of a representative open-water point in Bellingham Bay.
No live site files are changed. Requires numpy, requests and netCDF4.
"""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import tempfile
import numpy as np
import netCDF4
import requests

BASE = 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/nosofs/prod'
CYCLES = (21, 15, 9, 3)
CENTER_LAT, CENTER_LON = 48.69, -122.55
RADIUS_KM = 2.5


def runs(now):
    for offset in (0, 1):
        date = (now - dt.timedelta(days=offset)).strftime('%Y%m%d')
        for cycle in CYCLES:
            t = dt.datetime.strptime(date + f'{cycle:02d}', '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            if t + dt.timedelta(minutes=100) <= now:
                yield date, cycle, t


def download(url, path):
    total = 0
    with requests.get(url, stream=True, timeout=(20, 180), headers={'User-Agent': 'bellingham-conditions-temp-pilot/0.1'}) as response:
        response.raise_for_status()
        with path.open('wb') as dest:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 300 * 1024 * 1024:
                    raise ValueError('Unexpected NOAA file larger than 300 MiB')
                dest.write(chunk)
    if total < 1024 * 1024:
        raise ValueError('NOAA response unexpectedly small')
    return total


def masked_array(var, selections):
    arr = np.ma.asarray(var[tuple(selections)])
    return np.asarray(np.ma.filled(arr, np.nan), dtype=float)


def inspect(ds, date, cycle, valid_fallback, url):
    if 'temp' not in ds.variables:
        raise ValueError('temp field not present. Available fields: ' + ', '.join(ds.variables.keys()))
    temp = ds.variables['temp']
    dims = temp.dimensions
    units = str(getattr(temp, 'units', '')).strip()
    if units.lower() not in ('degrees c', 'degree_celsius', 'degrees_celsius', 'celsius', 'degree c', 'degc', 'degrees_c', 'degrees_celsius', 'degree_c'):
        raise ValueError(f'Unexpected temperature units {units!r}; refusing to interpret them as Celsius')
    if 'lat' not in ds.variables or 'lon' not in ds.variables:
        raise ValueError('Missing geographic coordinates')
    lat = np.asarray(ds.variables['lat'][:], dtype=float).ravel()
    lon = np.asarray(ds.variables['lon'][:], dtype=float).ravel()
    lon = np.where(lon > 180, lon - 360, lon)
    if len(lat) != len(lon):
        raise ValueError('Latitude/longitude dimension mismatch')
    # Local tangent-plane approximation appropriate at this short distance.
    north_km = (lat - CENTER_LAT) * 111.195
    east_km = (lon - CENTER_LON) * (111.195 * math.cos(math.radians(CENTER_LAT)))
    distance = np.hypot(north_km, east_km)
    ids = np.flatnonzero(np.isfinite(distance) & (distance <= RADIUS_KM))
    if len(ids) < 3:
        raise ValueError('Too few model nodes near requested Bellingham Bay sampling area')
    # Match the temperature's horizontal dimension to the latitude/longitude node array.
    node_axes = [i for i, (d, n) in enumerate(zip(dims, temp.shape)) if n == len(lat) and d.lower() in ('node', 'nodes', 'nnode')]
    if len(node_axes) != 1:
        raise ValueError(f'Could not identify temp node axis: dimensions={dims}, shape={temp.shape}, nNodes={len(lat)}')
    node_axis = node_axes[0]
    sigma_axes = [i for i, d in enumerate(dims) if d.lower() in ('siglay', 'sigma', 'layer', 'layers')]
    if len(sigma_axes) != 1:
        raise ValueError(f'Could not identify temp sigma-layer axis: {dims}')
    sigma_axis = sigma_axes[0]
    if 'siglay' not in ds.variables:
        raise ValueError('siglay coordinate missing; cannot verify surface layer')
    sigma_var = ds.variables['siglay']
    sigma_dims = sigma_var.dimensions
    sigma = np.ma.asarray(sigma_var[:]).filled(np.nan)
    if 'siglay' not in sigma_dims:
        raise ValueError(f'Cannot interpret siglay variable axes: {sigma_dims}')
    lay_axis = sigma_dims.index('siglay')
    per_layer_mean = np.nanmean(sigma, axis=tuple(i for i in range(sigma.ndim) if i != lay_axis))
    if not np.isfinite(per_layer_mean).any():
        raise ValueError('Sigma layers invalid')
    surface = int(np.nanargmax(per_layer_mean))  # FVCOM sigma nearest 0
    if not (-0.5 <= float(per_layer_mean[surface]) <= 0.05):
        raise ValueError(f'Selected layer does not appear near surface: {per_layer_mean[surface]}')
    selections = [0 if d.lower() in ('time', 'times') else slice(None) for d in dims]
    for i, d in enumerate(dims):
        if i == node_axis:
            selections[i] = ids
        elif i == sigma_axis:
            selections[i] = surface
        elif d.lower() not in ('time', 'times'):
            raise ValueError(f'Unrecognized non-spatial temp axis {d!r}: {dims}')
    values = masked_array(temp, selections).ravel()
    selected_lat = lat[ids]
    selected_lon = lon[ids]
    wet = np.ones(len(ids), dtype=bool)
    if 'wet_nodes' in ds.variables:
        w = ds.variables['wet_nodes']
        w_dims = w.dimensions
        w_sel = [ids if d.lower() in ('node','nodes','nnode') else 0 for d in w_dims]
        wet = np.asarray(w[tuple(w_sel)]).ravel() != 0
    elif 'wet_cells' in ds.variables:
        # wet_cells is element-centered; node wetness isn't directly available here.
        pass
    if len(values) != len(ids):
        raise ValueError('Temperature selection did not yield one value per node')
    valid = wet & np.isfinite(values) & (values > -2.5) & (values < 36)
    if valid.sum() < 3:
        raise ValueError(f'Not enough plausible surface temperatures: {int(valid.sum())}/{len(ids)}')
    if valid.sum() < max(3, int(.7 * len(ids))):
        raise ValueError('Too many invalid/land temperatures in sample area')
    # A robust median avoids spikes from a single nearshore model node.
    c = float(np.median(values[valid]))
    if 'Times' in ds.variables:
        row = np.asarray(ds.variables['Times'][0]).ravel()
        string_time = ''.join(x.decode('ascii') if isinstance(x, bytes) else str(x) for x in row).strip()
        valid_utc = dt.datetime.fromisoformat(string_time.replace(' ', 'T')[:19]).replace(tzinfo=dt.timezone.utc)
    elif 'time' in ds.variables:
        tv = ds.variables['time']
        d = netCDF4.num2date(tv[0], tv.units, only_use_cftime_datetimes=False)
        valid_utc = dt.datetime(d.year, d.month, d.day, d.hour, d.minute, d.second, tzinfo=dt.timezone.utc)
    else:
        valid_utc = valid_fallback
    return {
        'status': 'pilot_only_not_published',
        'source': 'NOAA SSCOFS FVCOM',
        'dataType': 'modeled_surface_temperature',
        'temperatureC': round(c, 2),
        'temperatureF': round(c * 9 / 5 + 32, 1),
        'modelValidUTC': valid_utc.isoformat().replace('+00:00','Z'),
        'modelCycleUTC': dt.datetime.strptime(date + f'{cycle:02d}', '%Y%m%d%H').replace(tzinfo=dt.timezone.utc).isoformat().replace('+00:00','Z'),
        'sampleCenter': {'lat': CENTER_LAT, 'lon': CENTER_LON},
        'sampleRadiusKm': RADIUS_KM,
        'nodesInArea': int(len(ids)),
        'validNodes': int(valid.sum()),
        'temperatureRangeC': [round(float(np.min(values[valid])),2), round(float(np.max(values[valid])),2)],
        'selectedSurfaceLayer': surface,
        'sigmaAtSelectedLayer': round(float(per_layer_mean[surface]),4),
        'temperatureDimensions': list(dims),
        'temperatureUnitsOriginal': units,
        'sourceFile': url,
        'note': 'Modeled guidance, not a measured observation. Pilot result only; do not use for navigation.'
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sample', help='Optional NOAA NetCDF file to inspect without downloading')
    p.add_argument('--output', default='water-temp-pilot.json')
    args = p.parse_args()
    if args.sample:
        with netCDF4.Dataset(args.sample) as ds:
            report = inspect(ds, 'sample', 3, dt.datetime.now(dt.timezone.utc), 'local sample')
    else:
        now = dt.datetime.now(dt.timezone.utc)
        report = None
        errors = []
        with tempfile.TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / 'sscofs-temperature.nc'
            for date, cycle, nominal in runs(now):
                url = f'{BASE}/sscofs.{date}/sscofs.t{cycle:02d}z.{date}.fields.f003.nc'
                try:
                    print(f'Trying {url}', flush=True)
                    size = download(url, dest)
                    print(f'Downloaded {size/1048576:.1f} MiB', flush=True)
                    with netCDF4.Dataset(dest) as ds:
                        report = inspect(ds, date, cycle, nominal + dt.timedelta(hours=3), url)
                    break
                except Exception as exc:
                    errors.append(str(exc))
                    print(f'Candidate failed: {exc}', flush=True)
                    dest.unlink(missing_ok=True)
        if report is None:
            raise SystemExit('No usable NOAA temperature forecast. Last errors: ' + '; '.join(errors[-3:]))
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)

if __name__ == '__main__':
    main()
