
export async function onRequestGet() {
  const url = new URL(
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
  );

  url.search = new URLSearchParams({
    date: "latest",
    station: "9449211",
    product: "water_level",
    datum: "MLLW",
    time_zone: "gmt",
    units: "english",
    format: "json",
    application: "bellingham_bay_conditions"
  }).toString();

  try {
    const response = await fetch(url.toString(), {
      cf: { cacheTtl: 300, cacheEverything: true }
    });

    if (!response.ok) {
      throw new Error(`NOAA returned ${response.status}`);
    }

    const data = await response.json();

    if (data.error) {
      throw new Error(data.error.message || "NOAA API error");
    }

    const readings = data.data || [];
    const latest = readings[readings.length - 1];

    if (!latest || !Number.isFinite(Number(latest.v))) {
      throw new Error("No valid water level available");
    }

    return Response.json({
      available: true,
      station: "Bellingham, WA",
      stationId: "9449211",
      waterLevel: Number(latest.v),
      units: "ft",
      datum: "MLLW",
      observationTime: latest.t + " UTC",
      source: "NOAA CO-OPS",
      sourceUrl:
        "https://tidesandcurrents.noaa.gov/stationhome.html?id=9449211"
    }, {
      headers: { "Cache-Control": "public, max-age=300" }
    });

  } catch (error) {
    return Response.json({
      available: false,
      station: "Bellingham, WA",
      waterLevel: null,
      error: error.message
    }, {
      status: 503,
      headers: { "Cache-Control": "no-store" }
    });
  }
}
