// Bellingham Bay Conditions: local pressure-gradient reference indicator.
// NOAA AWC METAR historical lookup. No persistent storage or paid dependencies.
const API = 'https://aviationweather.gov/api/data/metar?ids=KAWO,KORS&format=json&hours=3';
const MAX_PAIR_GAP_MS = 35 * 60 * 1000;
const CURRENT_AGE_MS = 90 * 60 * 1000;
const HISTORICAL_AGE_MS = 180 * 60 * 1000;
const FUTURE_TOLERANCE_MS = 10 * 60 * 1000;

function parseObservation(record, now) {
  if (!record || !['KAWO','KORS'].includes(record.icaoId)) return null;
  const raw = String(record.rawOb || '');
  const match = raw.match(/(?:^|\s)A(\d{4})(?=\s|$)/);
  let pressure = match ? Number(match[1]) / 100 : null;
  if (pressure === null && record.altim != null && Number.isFinite(Number(record.altim))) {
    pressure = Number(record.altim) / 33.8638866667;
  }
  const numericTime = Number(record.obsTime);
  const ms = record.obsTime != null && Number.isFinite(numericTime) && numericTime > 1e9
    ? numericTime * (numericTime > 1e11 ? 1 : 1000)
    : Date.parse(record.reportTime || '');
  if (!Number.isFinite(ms) || !Number.isFinite(pressure) || pressure < 27 || pressure > 33) return null;
  if (ms > now + FUTURE_TOLERANCE_MS || now - ms > HISTORICAL_AGE_MS) return null;
  return {
    station: record.icaoId,
    name: record.icaoId === 'KAWO' ? 'Arlington Municipal Airport' : 'Eastsound / Orcas Island Airport',
    pressureInHg: Number(pressure.toFixed(3)),
    observationTime: new Date(ms).toISOString(),
    ageMinutes: Math.max(0, Math.round((now-ms)/60000)), ms
  };
}
function publicStation(r) {
  return r ? {station:r.station,name:r.name,pressureInHg:r.pressureInHg,observationTime:r.observationTime,ageMinutes:r.ageMinutes} : null;
}
function windGuideline(d) {
  if (d < .01) return 'Below the local reference range';
  if (d < .025) return 'Typically below 10 mph (local rule of thumb)';
  if (d < .045) return 'Typically 10–15 mph (local rule of thumb)';
  if (d < .065) return 'Typically 15–25 mph (local rule of thumb)';
  if (d < .105) return 'Typically 25–30 mph (local rule of thumb)';
  if (d < .125) return 'Typically 30+ mph (local rule of thumb)';
  return 'Above the local reference range; squalls and rapid changes possible';
}
function chooseMatchedPair(records) {
  const a = records.filter(r=>r.station==='KAWO').sort((x,y)=>y.ms-x.ms);
  const b = records.filter(r=>r.station==='KORS').sort((x,y)=>y.ms-x.ms);
  const candidates = [];
  for (const aw of a) for (const or of b) {
    const gap=Math.abs(aw.ms-or.ms);
    if (gap <= MAX_PAIR_GAP_MS) candidates.push({aw,or,gap,
      // Pair is only as recent as its older member.
      olderTime:Math.min(aw.ms,or.ms),newerTime:Math.max(aw.ms,or.ms)});
  }
  // Prefer the most recent complete matched pair, then the tighter time gap.
  candidates.sort((x,y)=>y.olderTime-x.olderTime || x.gap-y.gap);
  return candidates[0] || null;
}
function respond(data) {
  return new Response(JSON.stringify(data,null,2), {headers:{
    'content-type':'application/json; charset=utf-8',
    'cache-control':'public, max-age=120, s-maxage=300'
  }});
}
export async function onRequestGet() {
  const base={available:false,dataType:'airport_pressure_gradient',gradientDefinition:'Arlington (KAWO) minus Eastsound (KORS)',
    units:'inHg',source:'NOAA Aviation Weather Center METAR observations',
    sourceUrl:'https://aviationweather.gov/data/api/',
    localReferenceUrl:'https://surfski.wiki/bellingham_local_conditions',
    guidance:'Informal local heuristic only for predominantly southerly winds; not an official forecast or safety assessment.',
    pairGapLimitMinutes:35};
  try {
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),8500);
    let response;
    try { response=await fetch(API,{headers:{Accept:'application/json','User-Agent':'BellinghamBayConditions/1.1 (public marine conditions)'},signal:controller.signal}); }
    finally {clearTimeout(timeout);}
    if (!response.ok) return respond({...base,status:'upstream_error',note:`NOAA response HTTP ${response.status}`});
    const payload=await response.json();
    if(!Array.isArray(payload)) return respond({...base,status:'invalid_response',note:'Unexpected NOAA METAR response'});
    const now=Date.now();
    const records=payload.map(r=>parseObservation(r,now)).filter(Boolean);
    const latest=(id)=>records.filter(r=>r.station===id).sort((x,y)=>y.ms-x.ms)[0] || null;
    const latestStations=[publicStation(latest('KAWO')),publicStation(latest('KORS'))];
    const pair=chooseMatchedPair(records);
    if(!pair) return respond({...base,status:'no_matched_pair',stations:latestStations,
      note:'No matching Arlington and Eastsound observations within 35 minutes in the last three hours.'});
    const stations=[publicStation(pair.aw),publicStation(pair.or)];
    const pairAgeMinutes=Math.max(0,Math.round((now-pair.olderTime)/60000));
    const status=(now-pair.olderTime <= CURRENT_AGE_MS)?'current':'earlier';
    const gradient=Number((pair.aw.pressureInHg-pair.or.pressureInHg).toFixed(3));
    const direction=gradient>0?'South-favoring pressure pattern':gradient<0?'Not south-favoring':'Near-zero pressure difference';
    return respond({...base,available:true,status,
      gradientInHg:gradient,gradientMagnitudeInHg:Number(Math.abs(gradient).toFixed(3)),
      direction,approximateNextHourSouthWind:gradient>0?windGuideline(gradient):null,
      stations,latestStations, gapMinutes:Math.round(pair.gap/60000),
      pairAgeMinutes, pairOldestObservationTime:new Date(pair.olderTime).toISOString(),
      pairNewestObservationTime:new Date(pair.newerTime).toISOString(),
      selection:'latest_synchronized_pair',
      note:status==='earlier'
        ?'Earlier matched observations, not current conditions. The local wind interpretation refers to the earlier observation period.'
        :'Matched station observations. Interpret only in context of a predominantly southerly flow.'});
  } catch(e) {return respond({...base,status:'unavailable',note:'Unable to retrieve airport observations.'});}
}

