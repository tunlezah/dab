# DAB+ Scanning Pipeline — Implementation Tracking

## Priority 1: Adaptive Dwell Time
- [ ] Add `MIN_DWELL_TIME`, `MAX_DWELL_TIME`, `DWELL_POLL_INTERVAL` to `config.py`
- [ ] Replace fixed `asyncio.sleep(SCAN_DWELL_TIME)` with adaptive polling loop in `scanner.py`
- [ ] Add stabilisation check (two consecutive polls with same service count)

## Priority 2: SDR Gain Control
- [ ] Add `SDR_GAIN`, `SDR_AGC`, `SDR_PPM` to `config.py`
- [ ] Pass `-G`, `-Q`, `-p` flags to welle-cli in `welle_manager.py`
- [ ] Add `GET/POST /api/sdr/config` endpoints in `routes.py`

## Priority 3: Retry Logic
- [ ] Collect failed channels in `retry_queue` after first pass
- [ ] Run second pass over `retry_queue` (max 1 retry per channel)
- [ ] Add `attempts` field to station results
- [ ] Check welle-cli health before each tune

## Priority 4: FIC Race Condition Fix
- [ ] Add deferred label resolution (placeholder `[SID:0x{sid:04X}]`)
- [ ] Update labels on subsequent polls within dwell window
- [ ] Distinguish empty-channel vs incomplete-decode in logging

## Priority 5: Structured Error Handling
- [ ] Replace bare `except Exception` with typed catches
- [ ] Add per-channel scan status tracking (`scan_report`)
- [ ] Add `GET /api/scan/report` endpoint
- [ ] Log SNR/signal quality if available

## Priority 6: Duplicate SID Handling
- [ ] Change `station_registry.py` to not blindly overwrite
- [ ] Keep first occurrence (or better SNR) when duplicate SID found
- [ ] Store alternate channels list
- [ ] Log duplicate detections

## Priority 7: Data-Only Service Filtering
- [ ] Add `INCLUDE_DATA_SERVICES` config flag
- [ ] Check `ASCTy`/component transport mode for audio vs data
- [ ] Mark data-only services as `type: "data"`
- [ ] Exclude from default station list

## Documentation & Versioning
- [ ] Update version to 2.0.0
- [ ] Update README.md with new config variables and endpoints
- [ ] Update install.sh version

## Testing
- [ ] Update test_scanner.py for adaptive dwell and retry logic
- [ ] Update test_routes.py for new endpoints
- [ ] Update test_station_registry.py for duplicate handling
- [ ] Run full test suite

## Review
- [ ] Verify all changes compile and tests pass
- [ ] Commit and push
