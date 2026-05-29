# Authentication

## Google Earth Engine (GEE)

GEE is required for global products (CHIRPS, ERA5-Land, MOD16, GLO-30, …).

### One-time setup

```bash
pip install aihydro-data[gee]
python -c "import ee; ee.Authenticate()"   # opens browser OAuth flow
```

After authentication, a credentials file is written to
`~/.config/earthengine/credentials`.

### Verify

```python
import ee
ee.Initialize()
print("GEE OK")
```

Or via MCP: the `data_fetch()` call will surface
`{"error": True, "code": "GEE_AUTH_MISSING", "recovery": "..."}` if GEE is not
initialised — follow the `recovery` field instructions.

## HyRiver (pygridmet, pygeohydro, py3dep)

HyRiver backends need no authentication — they talk to USGS and Daymet HTTP APIs
with anonymous requests. Just install the extra:

```bash
pip install aihydro-data[hyriver]
```

## USGS NWIS (direct_api)

Anonymous — no credentials needed.

```bash
pip install aihydro-data[hyriver]   # dataretrieval ships with hyriver
```

## CHIRPS IRI OPeNDAP (auth-free fallback)

No credentials. Requires `xarray` + `netCDF4` for OPeNDAP support:

```bash
pip install aihydro-data[opendap]   # xarray + netCDF4
```

Coverage: 50°S–50°N (land only). Use as last-resort fallback when GEE is unavailable.

## STAC (Planetary Computer, Element84)

Planetary Computer requires an API key for high-volume access but works
anonymously for small requests. The `stac` backend handles signing automatically
when `planetary-computer` is installed.

```bash
pip install aihydro-data[stac]
```

## Troubleshooting

| Error code          | Cause                               | Fix                                      |
|---------------------|-------------------------------------|------------------------------------------|
| `GEE_AUTH_MISSING`  | No GEE credentials on disk          | `ee.Authenticate()` then `ee.Initialize()` |
| `GEE_QUOTA_EXCEEDED`| Too many requests in a short window | Wait ~30s or reduce `max_workers` in batch |
| `MISSING_EXTRAS`    | Backend package not installed       | `pip install aihydro-data[<extra>]`      |
