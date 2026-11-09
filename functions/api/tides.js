
export async function onRequestGet() {
  const now = new Date();

  const formatDate = date =>
    date.toISOString().slice(0, 10).replace(/-/g, "");

  const end = new Date(now.getTime() + 3 * 86400000);

  const url = new URL(
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
  );

  url.search = new URLSearchParams({
    begin_date: formatDate(now),
    end_date: formatDate(end),
    station: "9449211",
    product: "predictions",
    interval: "hilo",
    datum: "MLLW",
    time_zone: "gmt",
    units: "english",
    format: "json",
    application: "bellingham_bay_conditions"
  }).toString();

  try {
    const response = await fetch(url.toString(), {
      cf: { cacheTtl: 900, cacheEverything: true }
    });

    if (!response.ok) {
      throw new Error(`NOAA returned ${response.status}`);
    }

    const data = await response.json();

    if (data.error) {
      throw new Error(data.error.message || "NOAA API error");
    }

    const predictions = (data.predictions || [])
      .map(p => ({
        type: p.type === "H" ? "High" : "Low",
        height: Number(p.v),
        time: p.t.replace(" ", "T") + "Z"
      }))
      .filter(p =>
        Number.isFinite(p.height) &&
        Number.isFinite(Date.parse(p.time))
      );

    const upcoming = predictions.filter(
      p => Date.parse(p.time) > now.getTime()
    );

    if (!upcoming.length) {
      throw new Error("No upcoming tide predictions found");
    }

    return Response.json({
      available: true,
      station: "Bellingham, WA",
      stationId: "9449211",
      dataType: "prediction",
      datum: "MLLW",
      units: "ft",
      nextTide: upcoming[0],
      upcomingTides: upcoming.slice(0, 6),
      source: "NOAA CO-OPS",
      sourceUrl:
        "https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id=9449211"
    }, {
      headers: {
        "Cache-Control": "public, max-age=300"
      }
    });

  } catch (error) {
    return Response.json({
      available: false,
      station: "Bellingham, WA",
      nextTide: null,
      error: error.message
    }, {
      status: 503,
      headers: {
        "Cache-Control": "no-store"
      }
    });
  }
}

}
