const BUOY_URL = 'https://www.ndbc.noaa.gov/data/realtime2/46118.txt';
const AIRPORT_URL = 'https://aviationweather.gov/api/data/metar?ids=KBLI&format=json';
const MINUTE = 60000;
const KNOTS_TO_MPH = 1.150779448;
const METERS_PER_SECOND_TO_MPH = 2.2369362921;

const numeric = v => (v === null || v === undefined || v === '' || v === 'MM' || v === '999' || v === '999.0') ? null : (Number.isFinite(Number(v)) ? Number(v) : null);
const rounded = n => n == null ? null : Math.round(n * 10) / 10;
const cacheHeaders = { 'Cache-Control': 'public, max-age=300' };

function makeTime(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
  if (typeof value !== 'string' || !value) return NaN;
  return Date.parse(value);
}
function ageInMinutes(timestamp, now) { return Math.round((now - timestamp) / MINUTE); }
function isRecent(timestamp, now, minutes) {
  const age = (now - timestamp) / MINUTE;
  return Number.isFinite(age) && age >= -10 && age <= minutes;
}

async function buoyObservation(now) {
  const response = await fetch(BUOY_URL, { cf: { cacheTtl: 300, cacheEverything: true }, headers: { 'User-Agent': 'BellinghamBayConditions/1.0 (public conditions dashboard)' } });
  if (!response.ok) throw new Error('NDBC HTTP ' + response.status);
  const lines = (await response.text()).trim().split(/\r?\n/);
  if (lines.length < 3) throw new Error('NDBC data is empty');
  const headings = lines[0].replace(/^#/, '').trim().split(/\s+/);
  const get = key => headings.indexOf(key);
  const y = get('YY') >= 0 ? get('YY') : get('YYYY');
  const idx = { y, month: get('MM'), day: get('DD'), hour: get('hh'), minute: get('mm'), speed: get('WSPD'), gust: get('GST'), dir: get('WDIR') };
  if (Object.values(idx).some(x => x < 0)) throw new Error('Unexpected NDBC format');
  const candidates = [];
  for (const line of lines.slice(2)) {
    if (!line.trim() || line.startsWith('#')) continue;
    const fields = line.trim().split(/\s+/);
    let year = numeric(fields[idx.y]);
    if (year == null) continue;
    if (year < 100) year += 2000;
    const mo = numeric(fields[idx.month]), day = numeric(fields[idx.day]);
    const hour = numeric(fields[idx.hour]), minute = numeric(fields[idx.minute]);
    if ([mo, day, hour, minute].some(v => v == null)) continue;
    const time = Date.UTC(year, mo - 1, day, hour, minute);
    const speed = numeric(fields[idx.speed]), gust = numeric(fields[idx.gust]);
    const dir = numeric(fields[idx.dir]);
    if (speed == null && gust == null) continue;
    if (time > now + 10 * MINUTE || !Number.isFinite(time)) continue;
    candidates.push({ time, speed, gust, dir });
  }
  if (!candidates.length) throw new Error('NDBC has no valid wind observations');
  candidates.sort((a,b) => b.time - a.time);
  const latest = candidates[0];
  return {
    station: "Se'lhaem - Bellingham Bay", stationId: '46118', source: 'NOAA NDBC',
    sourceUrl: 'https://www.ndbc.noaa.gov/station_page.php?station=46118',
    windSpeed: rounded(latest.speed == null ? null : latest.speed * METERS_PER_SECOND_TO_MPH),
    windGust: rounded(latest.gust == null ? null : latest.gust * METERS_PER_SECOND_TO_MPH),
    windDirection: latest.dir,
    observationTime: new Date(latest.time).toISOString(),
    ageMinutes: ageInMinutes(latest.time, now),
    fresh: isRecent(latest.time, now, 120)
  };
}

async function airportObservation(now) {
  const response = await fetch(AIRPORT_URL, { cf: { cacheTtl: 300, cacheEverything: true }, headers: { 'Accept': 'application/json', 'User-Agent': 'BellinghamBayConditions/1.0 (public conditions dashboard)' } });
  if (!response.ok) throw new Error('AWC HTTP ' + response.status);
  const data = await response.json();
  if (!Array.isArray(data) || !data.length) throw new Error('No KBLI METAR available');
  const records = data.filter(row => row && row.icaoId === 'KBLI').map(row => ({ ...row, timestamp: makeTime(row.obsTime ?? row.reportTime) })).filter(row => Number.isFinite(row.timestamp)).sort((a,b) => b.timestamp - a.timestamp);
  if (!records.length) throw new Error('KBLI has no timestamped observations');
  const row = records[0];
  if (!isRecent(row.timestamp, now, 90)) throw new Error('KBLI observation is stale');
  const speed = numeric(row.wspd), gust = numeric(row.wgst), direction = numeric(row.wdir);
  if (speed == null) throw new Error('KBLI wind speed unavailable');
  return {
    station: 'Bellingham International Airport', stationId: 'KBLI',
    source: 'NOAA Aviation Weather Center METAR',
    sourceUrl: 'https://tgftp.nws.noaa.gov/weather/current/KBLI.html',
    windSpeed: rounded(speed * KNOTS_TO_MPH),
    windGust: rounded(gust == null ? null : gust * KNOTS_TO_MPH),
    windDirection: direction,
    observationTime: new Date(row.timestamp).toISOString(),
    ageMinutes: ageInMinutes(row.timestamp, now), fresh: true
  };
}

export async function onRequestGet() {
  const now = Date.now();
  let buoy = null, buoyError = null, airport = null, airportError = null;
  try { buoy = await buoyObservation(now); } catch (e) { buoyError = String(e.message || e); }
  // Avoid requesting the airport at all when the preferred marine station is fresh.
  if (buoy && buoy.fresh) {
    return Response.json({ available: true, status: 'current', dataType: 'marine_observation', usingFallback: false,
      station: buoy.station, stationId: buoy.stationId, source: buoy.source, sourceUrl: buoy.sourceUrl,
      windSpeed: buoy.windSpeed, windGust: buoy.windGust, windDirection: buoy.windDirection,
      observationTime: buoy.observationTime, ageMinutes: buoy.ageMinutes, units: 'mph' }, { headers: cacheHeaders });
  }
  try { airport = await airportObservation(now); } catch (e) { airportError = String(e.message || e); }
  if (airport) {
    return Response.json({ available: true, status: 'fallback', dataType: 'airport_observation', usingFallback: true,
      station: airport.station, stationId: airport.stationId, source: airport.source, sourceUrl: airport.sourceUrl,
      windSpeed: airport.windSpeed, windGust: airport.windGust, windDirection: airport.windDirection,
      observationTime: airport.observationTime, ageMinutes: airport.ageMinutes, units: 'mph',
      marineBuoyStatus: buoy ? 'stale' : 'unavailable',
      marineBuoyLastObservation: buoy?.observationTime || null,
      marineBuoyAgeMinutes: buoy?.ageMinutes ?? null,
      note: 'Land-based airport winds may differ substantially from winds over Bellingham Bay.' }, { headers: cacheHeaders });
  }
  return Response.json({ available: false, status: buoy ? 'stale' : 'unavailable',
    windSpeed: null, windGust: null, windDirection: null, units: 'mph',
    station: "Se'lhaem - Bellingham Bay", stationId: '46118',
    observationTime: buoy?.observationTime || null, ageMinutes: buoy?.ageMinutes ?? null,
    source: 'NOAA NDBC', sourceUrl: 'https://www.ndbc.noaa.gov/station_page.php?station=46118',
    marineBuoyStatus: buoy ? 'stale' : 'unavailable', marineBuoyLastObservation: buoy?.observationTime || null,
    note: 'Marine buoy and alternate airport data are currently unavailable or outdated.',
    diagnostics: { buoy: buoyError || 'Older than 120 minutes', airport: airportError || 'unavailable' } }, { headers: cacheHeaders });
}
