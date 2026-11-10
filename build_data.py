"""Convert a NOAA SSCOFS subset NetCDF/HDF5 file into compact native-mesh JSON.
Usage: python build_data.py /path/to/noaa-subset.nc
Dependencies: numpy, h5py. This script is for preparation, not browser runtime.
"""
import json
import sys
from pathlib import Path
import h5py
import numpy as np

source=Path(sys.argv[1])
with h5py.File(source) as f:
    lat=f['lat'][:].astype(float)
    lon=f['lon'][:].astype(float)
    lon=np.where(lon>180,lon-360,lon)
    tris=f['nv'][:].T.astype(int)-1
    u=f['u'][:].astype(float)
    v=f['v'][:].astype(float)
    wet=f['wet_cells'][:].astype(bool)
    times=[b''.join(row).decode('ascii').strip().replace(' ','T')[:19]+'Z' for row in f['Times'][:]]
    n=len(lat)
    result=[]
    for t in range(len(times)):
        accum_u=np.zeros(n);accum_v=np.zeros(n);counts=np.zeros(n)
        valid=wet[t] & np.isfinite(u[t]) & np.isfinite(v[t]) & (np.abs(u[t])<10) & (np.abs(v[t])<10)
        for col in range(3):
            np.add.at(accum_u,tris[valid,col],u[t,valid]);np.add.at(accum_v,tris[valid,col],v[t,valid]);np.add.at(counts,tris[valid,col],1)
        counts[counts==0]=1
        vals=np.column_stack([np.rint(accum_u/counts*1000).astype(int),np.rint(accum_v/counts*1000).astype(int)])
        result.append(vals.tolist())
    data={
       'metadata':{'model':'NOAA SSCOFS FVCOM','kind':'historical modeled nowcast','velocityUnits':'millimeters per second','currentDatum':'surface','note':'Original NOAA water-only triangular mesh; velocities interpolated from adjacent wet cells to mesh vertices. Do not use for navigation.', 'cells':int(len(tris)),'nodes':int(n)},
       'times':times,
       'nodes':np.round(np.column_stack([lat,lon]),6).tolist(),
       'triangles':tris.tolist(),
       'vectors':result
    }
output=Path(__file__).parent/'currents-v2-data.json'
output.write_text(json.dumps(data,separators=(',',':')))
print(output,output.stat().st_size,'bytes',len(times),'hours',len(tris),'triangles')
