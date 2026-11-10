// Cloudflare Pages Function. Standalone pilot; does not affect existing APIs.
const API = 'https://aviationweather.gov/api/data/metar?ids=KAWO,KORS&format=json';
const MAX_AGE_MS = 90 * 60 * 1000;
const MAX_PAIR_GAP_MS = 35 * 60 * 1000;

function parseObservation(record) {
  if (!record || !['KAWO', 'KORS'].includes(record.icaoId)) return null;
  const raw = String(record.rawOb || '');
  // METAR A#### reports altimeter in hundredths of inches Hg.
  const match = raw.match(/(?:^|\s)A(\d{4})(?=\s|$)/);
  let inHg = match ? Number(match[1]) / 100 : null;
  // JSON altim field is hPa, not inches Hg.
  if (inHg === null && Number.isFinite(Number(record.altim))) {
    inHg = Number(record.altim) / 33.8638866667;
  }
  const ms = typeof record.obsTime === 'number' && record.obsTime > 1000000000
    ? record.obsTime * (record.obsTime > 1e11 ? 1 : 1000)
    : Date.parse(record.reportTime || '');
  if (!Number.isFinite(ms) || !Number.isFinite(inHg) || inHg < 27 || inHg > 33) return null;
  return {
    station: record.icaoId,
    name: record.icaoId === 'KAWO' ? 'Arlington Municipal Airport' : 'Eastsound / Orcas Island Airport',
    pressureInHg: Number(inHg.toFixed(3)),
    observationTime: new Date(ms).toISOString(),
    ageMinutes: Math.round((Date.now() - ms) / 60000),
    ms
  };
}

function bandForPositiveGradient(d) {
  // Source: surfski.wiki/bellingham_local_conditions; approximate, unvalidated heuristic.
  if (d < 0.01) return 'Below the wiki reference range';
  if (d < 0.025) return 'Typically below 10 mph (local rule of thumb)';
  if (d < 0.045) return 'Typically 10–15 mph (local rule of thumb)';
  if (d < 0.065) return 'Typically 15–25 mph (local rule of thumb)';
  if (d < 0.105) return 'Typically 25–30 mph (local rule of thumb)';
  if (d < 0.125) return 'Typically 30+ mph (local rule of thumb)';
  return 'Above reference range; squalls and rapid changes possible';
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {'content-type':'application/json; charset=utf-8', 'cache-control':'public, max-age=300, s-maxage=600'}
  });
}

export async function onRequestGet() {
  const base = {
    available: false,
    dataType: 'airport_pressure_gradient',
    gradientDefinition: 'Arlington (KAWO) minus Eastsound (KORS)',
    units: 'inHg',
    source: 'NOAA Aviation Weather Center METAR observations',
    sourceUrl: 'https://aviationweather.gov/data/api/',
    localReferenceUrl: 'https://surfski.wiki/bellingham_local_conditions',
    guidance: 'Informal local heuristic for predominantly southerly winds; not a wind forecast or a safety clearance.'
  };
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8500);
    let response;
    try {
      response = await fetch(API, {headers: {'Accept':'application/json', 'User-Agent':'BellinghamBayConditions/1.0 (public conditions pilot)'}, signal: controller.signal});
    } finally { clearTimeout(timeout); }
    if (!response.ok) return json({...base, status:'upstream_error', note:`NOAA response HTTP ${response.status}`});
    const payload = await response.json();
    if (!Array.isArray(payload)) return json({...base, status:'invalid_response', note:'NOAA did not return METAR records'});
    const records = payload.map(parseObservation).filter(Boolean);
    const latest = (station) => records.filter(r => r.station === station).sort((a,b) => b.ms - a.ms)[0];
    const arlington = latest('KAWO'), eastsound = latest('KORS');
    const stations = [arlington,eastsound].map(r => r ? ({station:r.station,name:r.name,pressureInHg:r.pressureInHg,observationTime:r.observationTime,ageMinutes:r.ageMinutes}) : null);
    if (!arlington || !eastsound) return json({...base, status:'missing_station', stations, note:'Both station pressures are required'});
    const now = Date.now();
    if ([arlington,eastsound].some(r => (now-r.ms)<-10*60*1000 || (now-r.ms)>MAX_AGE_MS))
      return json({...base,status:'stale',stations,note:'At least one airport observation is too old (>90 minutes) or has an invalid future timestamp'});
    const gapMinutes = Math.round(Math.abs(arlington.ms-eastsound.ms)/60000);
    if (gapMinutes*60000 > MAX_PAIR_GAP_MS)
      return json({...base,status:'time_mismatch',stations,gapMinutes,note:'Station observations are more than 35 minutes apart'});
    const delta = Number((arlington.pressureInHg-eastsound.pressureInHg).toFixed(3));
    const favorsSouth = delta > 0;
    return json({ ...base, available: true, status:'current', stations,
      gradientInHg: delta, gradientMagnitudeInHg: Number(Math.abs(delta).toFixed(3)), gapMinutes,
      direction: favorsSouth ? 'South-favoring pressure pattern' : delta < 0 ? 'Not south-favoring' : 'Near zero',
      approximateNextHourSouthWind: favorsSouth ? bandForPositiveGradient(delta) : null,
      note: favorsSouth ? 'Apply only when regional winds are predominantly southerly. Pressure gradient alone cannot determine actual bay wind speed.' : 'The local south-wind lookup should not be used for this pressure-gradient sign.'
    });
  } catch (e) {
    return json({...base,status:'unavailable', note:'Unable to retrieve NOAA airport observations; try again later.'});
  }
}
