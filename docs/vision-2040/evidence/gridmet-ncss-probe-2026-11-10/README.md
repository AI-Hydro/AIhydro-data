# GridMET NCSS probe, 2026-11-10 (aihydro-data PR "GridMET large box / leap day")

Live requests to `thredds.northwestknowledge.net` (approved host), precipitation (`pr`), 1.5 s apart. **10 requests in all**:
7 diagnostic probes (`requests.jsonl`, `probe.py`) + 3 for the check through the new client (`run_live.py`, `live_result.json`:
WY2000 = 2 requests, calendar 2000 = 1). Budget was 50. These are transport checks, not scientific results.

The rehearsal (FINDINGS F1) read "GridMET did NOT process your request successfully" as an NCSS error body for boxes of 0.18 deg and larger,
and pygridmet's docstring says GridMET drops 31 Dec of leap years. This artifact is the evidence that both readings are wrong.

## Probes (`requests.jsonl`; the sha256 column is the first 16 hex digits of the response body)
| label | box (deg) | days | HTTP | bytes | result |
|---|---|---|---|---|---|
| A (x3: runs 1-3, only the 3rd saved as `resp_A.nc`) | Fish River 0.893 x 0.632 | 2000-01-03 | 200 | 4260 | valid NetCDF-3, grid 16 lat x 23 lon, **9 of 368 cells NaN** (north-west corner, Canada) |
| B (x2; the 2nd saved as `resp_B.nc`) | 0.10 x 0.10 | **2000-12-31 only** | 200 | 3292 | valid NetCDF, `day = 2000-12-31`, 4 x 3 cells, 0 NaN, values 16.9-19.1 mm |
| D | 0.18 x 0.18 (rehearsal "fails") | 2000-01-01..03 | 200 | 3496 | valid NetCDF, 6 x 5 cells, **1 cell NaN on all days** |
| E | 0.15 x 0.15 (rehearsal "works") | 2000-01-01..03 | 200 | 3420 | valid NetCDF, 5 x 4 cells, **0 NaN** |

Every response is a NetCDF file (magic `CDF\x01`); none is an error body. The whole Fish River box is served in one request. The only
difference between the rehearsal's 0.15 and 0.18 boxes is that the larger one reaches a cell with no data (NaN), which pygridmet's NaN check
reads as a corrupt download (re-download 15 times, then its own ServiceError). The three A runs differ in sha256 only because the file
embeds per-request header text; shape and values are the same. `resp_A.nc` sha256 `7e4f8185...`, `resp_B.nc` `4af477a7...`,
`resp_D.nc` `3d8945c2...`, `resp_E.nc` `3591a800...` (full values in the `shasum` of the files).

## Check through the new client (`live_result.json`)
- Fish River box, WY2000 (1999-10-01..2000-09-30), 2 requests: **366 of 366 days, none missing**, 0 splits, 0 rejected responses,
  basin mean over **313 of 315** cells (cell centre in polygon; 2 have no data), 5.3 s.
- Calendar 2000, 1 request: **366 of 366 days**; 2000-12-31 present with basin mean 18.50 mm, 2000-12-30 0.01 mm (not a duplicated slice),
  2000-02-29 1.66 mm. 313 of 315 cells.
- WY2000 ends 2000-09-30, so it does not contain 31 Dec 2000; the calendar-2000 request is what shows the leap 31 December.

`run_live.py` assumes 7 prior requests when computing its cap (`PRIOR = 7`). Nothing here was written to `~/.aihydro`.
