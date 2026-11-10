// Cloudflare Pages Function: freshest local measured water temperature first;
// otherwise a clearly labeled, recent NOAA SSCOFS modeled surface value.
const SOURCES = [
  { name: 'University of Washington NWEM', base: 'https://nwem.apl.uw.edu/erddap/tabledap/bellinghambay_surfhydro' },
  { name: 'NANOOS ERDDAP (UW mirror)', base: 'https://erddap.nanoos.org/erddap/tabledap/bellinghambay_surfhydro' },
];
const OBSERVATION_FRESH_MIN = 360;
const MODEL_VALID_WINDOW_MIN = 540; // +-9 hours, accommodating six-hour scheduled updates.
const MODEL_PUBLICATION_MAX_MIN = 1080; // 18 hours from extraction.
const MODEL_CYCLE_MAX_MIN = 1080;
const STATION = "Se'lhaem - Bellingham Bay";
const responseJSON = data => Response.json(data, { headers: { 'Cache-Control': 'public, max-age=300' } });
const ageMinutes = (now, time) => Math.round((now - Date.parse(time)) / 60000);
const rounded = value => Math.round(value * 10) / 10;

async function observe(source, earliest) {
  const url = `${source.base}.json?time,sea_water_temperature&time>=${encodeURIComponent(earliest)}`;
  const r = await fetch(url, { cf: { cacheTtl: 600, cacheEverything: true }, signal: AbortSignal.timeout(8500) });
  if (!r.ok) throw Error(`ERDDAP HTTP ${r.status}`);
  const json = await r.json();
  const names = json.table?.columnNames || [];
  const ti = names.indexOf('time');
  const ci = names.indexOf('sea_water_temperature');
  if (ti < 0 || ci < 0) throw Error('Unexpected temperature columns');
  const observations = (json.table?.rows || []).map(row => ({ ms: Date.parse(row[ti]), c: row[ci] == null ? NaN : Number(row[ci]) }))
    .filter(v => Number.isFinite(v.ms) && Number.isFinite(v.c) && v.c >= -2 && v.c <= 35)
    .sort((a, b) => b.ms - a.ms);
  if (!observations.length) throw Error('No valid observations');
  return { ...observations[0], name: source.name, sourceUrl: source.base + '.html' };
}

async function getModeled(context, now) {
  // Read the static JSON from the *same* Cloudflare Pages deployment.
  const url = new URL('/water-temp-model.json', context.request.url);
  const response = await context.env.ASSETS.fetch(new Request(url, { method: 'GET' }));
  if (!response.ok) throw Error(`Model JSON not yet published (HTTP ${response.status})`);
  const m = await response.json();
  if (m.source !== 'NOAA SSCOFS FVCOM' || m.dataType !== 'modeled_surface_temperature' || m.status !== 'candidate') {
    throw Error('Unexpected modeled temperature data');
  }
  const validMs = Date.parse(m.modelValidUTC);
  const cycleMs = Date.parse(m.modelCycleUTC);
  const generatedMs = Date.parse(m.generatedUTC);
  const c = Number(m.temperatureC);
  const f = Number(m.temperatureF);
  if (![validMs, cycleMs, generatedMs, c, f].every(Number.isFinite) || c < -2 || c > 35 || Math.abs(f - (1.8 * c + 32)) > 0.16) {
    throw Error('Invalid modeled temperature numbers or times');
  }
  const validAge = Math.round((now - validMs) / 60000);
  const publicationAge = Math.round((now - generatedMs) / 60000);
  const cycleAge = Math.round((now - cycleMs) / 60000);
  if (Math.abs(validAge) > MODEL_VALID_WINDOW_MIN || publicationAge < -10 || publicationAge > MODEL_PUBLICATION_MAX_MIN || cycleAge < -10 || cycleAge > MODEL_CYCLE_MAX_MIN) {
    throw Error('Modeled temperature forecast is not sufficiently recent');
  }
  return {
    available: true, station: 'Bellingham Bay (central sample)',
    dataType: 'modeled_surface_temperature', status: 'modeled',
    waterTempF: rounded(f), waterTempC: rounded(c), units: 'F',
    observationTime: null, modelValidUTC: m.modelValidUTC,
    modelCycleUTC: m.modelCycleUTC, modelGeneratedUTC: m.generatedUTC,
    ageMinutes: validAge, source: 'NOAA SSCOFS FVCOM',
    sourceUrl: 'https://tidesandcurrents.noaa.gov/ofs/sscofs/sscofs.html',
    sampleCenter: m.sampleCenter, sampleRadiusKm: m.sampleRadiusKm,
    note: 'Modeled surface-water temperature, not a buoy observation. Not for navigation.',
  };
}

export async function onRequestGet(context) {
  const now = Date.now();
  const earliest = new Date(now - 168 * 3600000).toISOString();
  const results = await Promise.allSettled(SOURCES.map(s => observe(s, earliest)));
  const diagnostics = results.map((r, i) => ({
    source: SOURCES[i].name,
    ok: r.status === 'fulfilled',
    detail: r.status === 'fulfilled' ? 'Observation retrieved' : String(r.reason?.message || r.reason).slice(0, 140),
  }));
  const latest = results.filter(r => r.status === 'fulfilled').map(r => r.value)
    .filter(r => r.ms <= now + 10 * 60000).sort((a, b) => b.ms - a.ms)[0];
  const observedAge = latest ? Math.round((now - latest.ms) / 60000) : null;
  if (latest && observedAge >= -10 && observedAge <= OBSERVATION_FRESH_MIN) {
    return responseJSON({
      available: true, station: STATION, dataType: 'observation', status: 'current',
      waterTempF: rounded(latest.c * 1.8 + 32), waterTempC: rounded(latest.c), units: 'F',
      observationTime: new Date(latest.ms).toISOString(), ageMinutes: observedAge,
      source: latest.name, sourceUrl: latest.sourceUrl, diagnostics,
      note: 'Measured local temperature (~0.5 m depth).',
    });
  }
  try {
    const fallback = await getModeled(context, now);
    return responseJSON({ ...fallback, diagnostics,
      lastObservationTime: latest ? new Date(latest.ms).toISOString() : null,
      lastObservationAgeMinutes: observedAge,
    });
  } catch (error) {
    diagnostics.push({ source: 'NOAA SSCOFS modeled fallback', ok: false, detail: String(error.message).slice(0, 140) });
    return responseJSON({
      available: false, station: STATION, dataType: 'observation', status: latest ? 'stale' : 'unavailable',
      waterTempF: null, waterTempC: null, units: 'F',
      observationTime: latest ? new Date(latest.ms).toISOString() : null,
      ageMinutes: observedAge, source: latest ? latest.name : 'UW NWEM / NANOOS',
      sourceUrl: latest ? latest.sourceUrl : SOURCES[0].base + '.html', diagnostics,
      note: 'No sufficiently recent measured or modeled water temperature available.',
    });
  }
}
