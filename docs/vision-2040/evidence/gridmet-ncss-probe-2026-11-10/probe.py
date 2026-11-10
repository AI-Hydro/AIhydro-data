import sys, json, time, hashlib, requests
from urllib.parse import urlencode
BASE="http://thredds.northwestknowledge.net:8080/thredds/ncss/agg_met_pr_1979_CurrentYear_CONUS.nc"
LOG="/private/tmp/claude-501/-Users-mgalib-Documents-AI-Hydro/c31e1c57-abc3-4322-9112-432e4aca4c98/scratchpad/gridmet/live/requests.jsonl"
def req(label, w,s,e,n, t0, t1):
    q=dict(var="precipitation_amount",north=f"{n:.6f}",west=f"{w:.6f}",east=f"{e:.6f}",south=f"{s:.6f}",disableProjSubset="on",horizStride="1",time_start=t0+"T00:00:00Z",time_end=t1+"T00:00:00Z",timeStride="1",addLatLon="true",accept="netcdf")
    time.sleep(1.5)
    r=requests.get(BASE,params=q,timeout=120)
    b=r.content
    rec=dict(label=label,status=r.status_code,ctype=r.headers.get("content-type"),n=len(b),head=b[:8].hex(),sha=hashlib.sha256(b).hexdigest()[:16],body=(b[:300].decode("latin1") if not (b[:3]==b"CDF" or b[:4]==b"\x89HDF") else "<netcdf>"),url=r.url,w=e-w,h=n-s)
    open(LOG.replace("requests.jsonl","resp_"+label.split()[0]+".nc"),"wb").write(b)
    open(LOG,"a").write(json.dumps(rec)+"\n"); print(json.dumps({k:v for k,v in rec.items() if k!="url"})); return r
if __name__=="__main__":
    which=sys.argv[1]
    if which=="A": req("A fish 1d",-68.966,46.684,-68.073,47.316,"2000-01-03","2000-01-03")
    if which=="B": req("B dec31 2000 small",-68.60,47.20,-68.50,47.30,"2000-12-31","2000-12-31")
    if which=="C": req("C 0.15 full-year",-68.65,47.15,-68.50,47.30,"2000-01-01","2000-12-30")
    if which=="D": req("D 0.18 3d",-68.68,47.12,-68.50,47.30,"2000-01-01","2000-01-03")
    if which=="E": req("E 0.15 3d",-68.65,47.15,-68.50,47.30,"2000-01-01","2000-01-03")
