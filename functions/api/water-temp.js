
export async function onRequestGet() {
  const sourceUrl =
    "https://nwem.apl.uw.edu/erddap/tabledap/bellinghambay_surfhydro.json";

  // Retrieve observations from the previous 72 hours.
  const start = new Date(
    Date.now() - 72 * 60 * 60 * 1000
  ).toISOString();

  const query =
    "time,sea_water_temperature" +
    "&time>=" + start;

  const url = sourceUrl + "?" + query;

  try {
    const response = await fetch(url, {
      cf: {
        cacheTtl: 900,
        cacheEverything: true
      }
    });

    if (!response.ok) {
      throw new Error(
        "UW ERDDAP returned " + response.status
      );
    }

    const data = await response.json();

    const columns = data.table?.columnNames || [];
    const rows = data.table?.rows || [];

    const timeIndex = columns.indexOf("time");
    const tempIndex =
      columns.indexOf("sea_water_temperature");

    if (timeIndex < 0 || tempIndex < 0) {
      throw new Error("Unexpected ERDDAP data format");
    }

    const observations = rows
      .map(row => ({
        timestamp: Date.parse(row[timeIndex]),
        temperature: row[tempIndex] == null
          ? null
          : Number(row[tempIndex])
      }))
      .filter(item =>
        Number.isFinite(item.timestamp) &&
        item.temperature !== null &&
        Number.isFinite(item.temperature) &&
        item.temperature >= -2 &&
        item.temperature <= 35
      )
      .sort((a, b) => b.timestamp - a.timestamp);

    if (!observations.length) {
      throw new Error(
        "No valid temperature observations found"
      );
    }

    const latest = observations[0];

    const ageMinutes =
      (Date.now() - latest.timestamp) / 60000;

    // Accept only reasonably fresh observations.
    const fresh =
      ageMinutes >= -10 &&
      ageMinutes <= 360;

    const fahrenheit =
      latest.temperature * 9 / 5 + 32;

    return Response.json({
      available: fresh,
      station: "Se'lhaem - Bellingham Bay",
      dataType: "observation",
      waterTempF: fresh
        ? Math.round(fahrenheit * 10) / 10
        : null,
      waterTempC: fresh
        ? Math.round(latest.temperature * 10) / 10
        : null,
      units: "F",
      observationTime:
        new Date(latest.timestamp).toISOString(),
      ageMinutes: Math.round(ageMinutes),
      status: fresh ? "current" : "stale",
      source: "University of Washington NWEM",
      sourceUrl:
        "https://nwem.apl.uw.edu/erddap/tabledap/bellinghambay_surfhydro.html"
    }, {
      headers: {
        "Cache-Control": "public, max-age=900"
      }
    });

  } catch (error) {
    return Response.json({
      available: false,
      station: "Se'lhaem - Bellingham Bay",
      waterTempF: null,
      waterTempC: null,
      status: "unavailable",
      error: error.message
    }, {
      status: 503,
      headers: {
        "Cache-Control": "no-store"
      }
    });
  }
}
