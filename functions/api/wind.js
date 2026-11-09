
export async function onRequestGet() {
  const sourceUrl =
    "https://www.ndbc.noaa.gov/data/realtime2/46118.txt";

  const missing = value =>
    value === undefined ||
    value === "MM" ||
    value === "999" ||
    value === "999.0";

  const numberOrNull = value => {
    if (missing(value)) return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  };

  try {
    const response = await fetch(sourceUrl, {
      cf: {
        cacheTtl: 300,
        cacheEverything: true
      }
    });

    if (!response.ok) {
      throw new Error("NDBC returned " + response.status);
    }

    const text = await response.text();
    const lines = text.trim().split(/\r?\n/);

    // NDBC standard meteorological file:
    // First row contains column names, second contains units.
    const headers = lines[0]
      .replace(/^#/, "")
      .trim()
      .split(/\s+/);

    const index = name => headers.indexOf(name);

    const yearIndex = index("YY") >= 0
      ? index("YY")
      : index("YYYY");

    const required = [
      yearIndex,
      index("MM"),
      index("DD"),
      index("hh"),
      index("mm"),
      index("WSPD"),
      index("GST"),
      index("WDIR")
    ];

    if (required.some(i => i < 0)) {
      throw new Error("Unexpected NDBC data format");
    }

    const observations = [];

    for (const line of lines.slice(2)) {
      if (!line.trim() || line.startsWith("#")) continue;

      const fields = line.trim().split(/\s+/);

      const year = Number(fields[yearIndex]);
      const month = Number(fields[index("MM")]);
      const day = Number(fields[index("DD")]);
      const hour = Number(fields[index("hh")]);
      const minute = Number(fields[index("mm")]);

      const timestamp = Date.UTC(
        year < 100 ? year + 2000 : year,
        month - 1,
        day,
        hour,
        minute
      );

      const speed = numberOrNull(fields[index("WSPD")]);
      const gust = numberOrNull(fields[index("GST")]);
      const direction = numberOrNull(fields[index("WDIR")]);

      if (!Number.isFinite(timestamp)) continue;
      if (speed === null && gust === null) continue;

      observations.push({
        timestamp,
        speed,
        gust,
        direction
      });
    }

    observations.sort((a, b) => b.timestamp - a.timestamp);

    if (!observations.length) {
      throw new Error("No wind observations found");
    }

    const latest = observations[0];
    const ageMinutes =
      (Date.now() - latest.timestamp) / 60000;

    // Do not present stale or future-dated observations as live.
    const fresh = ageMinutes >= -10 && ageMinutes <= 120;

    const toMph = value =>
      value === null
        ? null
        : Math.round(value * 2.236936 * 10) / 10;

    return Response.json({
      available: fresh,
      station: "Se'lhaem - Bellingham Bay",
      stationId: "46118",
      windSpeed: fresh ? toMph(latest.speed) : null,
      windGust: fresh ? toMph(latest.gust) : null,
      windDirection: fresh ? latest.direction : null,
      units: "mph",
      observationTime:
        new Date(latest.timestamp).toISOString(),
      ageMinutes: Math.round(ageMinutes),
      status: fresh ? "current" : "stale",
      source: "NOAA NDBC",
      sourceUrl:
        "https://www.ndbc.noaa.gov/station_page.php?station=46118"
    }, {
      headers: {
        "Cache-Control": "public, max-age=300"
      }
    });

  } catch (error) {
    return Response.json({
      available: false,
      station: "Se'lhaem - Bellingham Bay",
      windSpeed: null,
      windGust: null,
      windDirection: null,
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
