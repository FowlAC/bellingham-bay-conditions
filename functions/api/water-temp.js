// Cloudflare Pages Function: /api/water-temp
// Use actual Se'lhaem buoy surface-water observations only.
// UW NWEM is primary. NANOOS mirrors the same instrument (not a distinct sensor).
const SOURCES = [
  {
    name: 'University of Washington NWEM',
    base: 'https://nwem.apl.uw.edu/erddap/tabledap/bellinghambay_surfhydro',
  },
  {
    name: 'NANOOS ERDDAP (UW mirror)',
    base: 'https://erddap.nanoos.org/erddap/tabledap/bellinghambay_surfhydro',
  },
];

const HOURS_FRESH = 6;
const HISTORY_HOURS = 168;
const STATION = "Se'lhaem - Bellingham Bay";

function publicJson(value, status = 200) {
  return Response.json(value, {
    status,
    headers: { 'Cache-Control': status === 200 ? 'public, max-age=600' : 'no-store' },
  });
}

async function readSource(source, earliest) {
  const url = new URL(`${source.base}.json`);
  // ERDDAP tabledap query: project variables and constrain by observation time.
  url.search = `time,sea_water_temperature&time>=${encodeURIComponent(earliest)}`;
  const response = await fetch(url.toString(), {
    cf: { cacheTtl: 600, cacheEverything: true },
    signal: AbortSignal.timeout(8500),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  const cols = data.table?.columnNames || [];
  const rows = data.table?.rows || [];
  const ti = cols.indexOf('time');
  const vi = cols.indexOf('sea_water_temperature');
  if (ti < 0 || vi < 0) throw new Error('Unexpected ERDDAP columns');
  const valid = rows.map(row => {
    const ms = Date.parse(row[ti]);
    const c = row[vi] == null ? NaN : Number(row[vi]);
    return { ms, c };
  }).filter(v => Number.isFinite(v.ms) && Number.isFinite(v.c) && v.c >= -2 && v.c <= 35)
    .sort((a, b) => b.ms - a.ms);
  if (!valid.length) throw new Error('No valid recent samples');
  return { ...valid[0], source: source.name, sourceUrl: `${source.base}.html` };
}

export async function onRequestGet() {
  const now = Date.now();
  const earliest = new Date(now - HISTORY_HOURS * 3600000).toISOString();
  const results = await Promise.allSettled(SOURCES.map(source => readSource(source, earliest)));
  const observations = results.filter(r => r.status === 'fulfilled').map(r => r.value)
    .filter(v => v.ms <= now + 10 * 60000).sort((a, b) => b.ms - a.ms);
  const latest = observations[0];
  const diagnostics = results.map((r, i) => ({
    source: SOURCES[i].name,
    ok: r.status === 'fulfilled',
    detail: r.status === 'rejected' ? String(r.reason?.message || r.reason).slice(0, 140) : 'Observation retrieved',
  }));
  if (!latest) {
    return publicJson({
      available: false, station: STATION, dataType: 'observation', waterTempF: null,
      waterTempC: null, observationTime: null, ageMinutes: null,
      status: 'unavailable', source: 'UW NWEM / NANOOS',
      sourceUrl: SOURCES[0].base + '.html', diagnostics,
      note: 'No recent valid local water-temperature observation; no substitute temperature shown.'
    });
  }
  const ageMinutes = Math.round((now - latest.ms) / 60000);
  const fresh = ageMinutes >= -10 && ageMinutes <= HOURS_FRESH * 60;
  const tempC = Math.round(latest.c * 10) / 10;
  const tempF = Math.round((latest.c * 9 / 5 + 32) * 10) / 10;
  return publicJson({
    available: fresh, station: STATION, dataType: 'observation',
    waterTempF: fresh ? tempF : null,
    waterTempC: fresh ? tempC : null,
    units: 'F', observationTime: new Date(latest.ms).toISOString(), ageMinutes,
    status: fresh ? 'current' : 'stale', source: latest.source,
    sourceUrl: latest.sourceUrl, diagnostics,
    note: fresh ? 'Measured surface water temperature (~0.5 m depth).' :
      'Local observation is too old to display as current. Historical observation withheld.'
  });
}
