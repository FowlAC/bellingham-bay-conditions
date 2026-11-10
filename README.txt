BELLINGHAM BAY CURRENT MAP - VERSION 2
=====================================

FILES TO UPLOAD TO GITHUB (repository root)
  currents-v2.html
  currents-v2-data.json

DO NOT delete or replace index.html, currents-preview.html,
or currents-sample.json. The V2 page is a separate preview.

After pushing to main and Cloudflare Pages deployment, open:
https://bellingham-bay-conditions.pages.dev/currents-v2.html

What's new:
- Original NOAA water-only triangular mesh; no dot-based rendering
- Smooth, interpolated speed shading on supported WebGL browsers
- Animated flow trails following model velocity field
- Speed legend in knots, show/hide toggles, and hourly playback
- Model timestamp automatically displayed in Pacific Time
- 2D triangle-shading fallback if WebGL is not available

IMPORTANT:
Data are historical NOAA SSCOFS model nowcasts for Nov 8-9 UTC,
NOT live ocean measurements and NOT safe for navigation.
The animation speeds up particle motion to aid visual interpretation.

For future automated updates, build_data.py demonstrates how the
NOAA NetCDF subset was turned into currents-v2-data.json.
Requires Python 3, numpy and h5py. This is not needed for deployment.

This map loads Leaflet and OpenStreetMap tiles from third parties.
Their availability and usage policies remain external dependencies.
