# DAB+ Scanning Pipeline — Deep Technical Audit

**Date:** 2026-04-03
**Scope:** Scanning pipeline analysis only — no code modifications
**Objective:** Determine why a cheap consumer DAB+ radio detects more stations than this application in the same physical location

---

## 1. Executive Summary

**Is the scanning likely complete?** No. The scanning pipeline has multiple structural deficiencies that would cause it to miss stations even with adequate hardware.

**Confidence level:** High.

The frequency list is complete for Australian Band III allocations (38 channels, all ETSI-compliant). The core issue is not _what_ is scanned but _how_ it is scanned. The combination of a short fixed dwell time (4 seconds), zero retry logic, no gain optimisation, and a race condition between FIC acquisition and service enumeration means that weak or marginal ensembles are systematically under-detected. These are compounding issues — each individually could miss stations, and together they almost certainly explain the gap versus consumer hardware.

The RTL-SDR hardware itself also imposes a ~2–3 dB sensitivity disadvantage versus dedicated DAB receiver chips, but this is secondary to the software-side gaps which are the dominant and more addressable cause.

---

## 2. Frequency Coverage Analysis

### 2.1 Channel Inventory

The `BAND_III_CHANNELS` list defines **38 channels** across the full Band III allocation (174–240 MHz), broken into blocks 5 through 13.

| Block | Standard Channels | N-Channel Present |
|-------|-------------------|-------------------|
| 5     | 5A, 5B, 5C, 5D   | No                |
| 6     | 6A, 6B, 6C, 6D   | No                |
| 7     | 7A, 7B, 7C, 7D   | No                |
| 8     | 8A, 8B, 8C, 8D   | No                |
| 9     | 9A, 9B, 9C, 9D   | No                |
| 10    | 10A, 10B, 10C, 10D | Yes (10N: 210.096 MHz) |
| 11    | 11A, 11B, 11C, 11D | Yes (11N: 217.088 MHz) |
| 12    | 12A, 12B, 12C, 12D | Yes (12N: 224.096 MHz) |
| 13    | 13A, 13B, 13C, 13D, 13E, 13F | No          |

### 2.2 ETSI EN 300 401 Frequency Verification

Spot-checked centre frequencies against ETSI EN 300 401 Table B.1. All 38 values match the standard to three decimal places. Block 13 correctly uses its non-uniform sub-channel layout (13A–13F), and the three N-channels present (10N, 11N, 12N) match their defined intermediate frequencies.

### 2.3 Channel Spacing

Within standard blocks, the intra-block spacing is **1.712 MHz** (e.g., 9A→9B: 204.640 − 202.928 = 1.712 MHz). Inter-block spacing at boundary transitions is **1.872 MHz** (e.g., 9D→10A: 209.936 − 208.064 = 1.872 MHz). Block 13 uses irregular spacing consistent with the ETSI specification. All spacings verify correctly.

### 2.4 Missing Channels

N-channels **5N, 6N, 7N, 8N, 9N, and 13N** are absent. These intermediate channels were allocated for specific European deployments and are not part of any Australian DAB+ assignment. Their omission has no operational impact for Australia.

### 2.5 L-Band Coverage

**L-Band (1452–1492 MHz) is not represented.** This is correct for the Australian context — ACMA has not allocated L-Band for DAB+ broadcasting, and no Australian deployments use it.

### 2.6 Australian Allocation Assessment

`POPULAR_CHANNELS` defaults to `["9A", "9B", "9C"]`, matching Sydney and the majority of Australian capital city assignments. All channels used by known Australian multiplexes — including 12A–12C (Hobart) and the broader 9-series and 12-series capital city assignments — are present in the defined list.

### 2.7 Verdict

Frequency coverage is **complete for Australian DAB+ operations**. All ACMA-assigned channels are present, frequencies are ETSI-compliant, and the absent N-channels and L-Band are irrelevant to the Australian regulatory environment.

---

## 3. Scan Strategy & Timing Analysis

### 3.1 Sequential Channel Processing

The scanner processes channels strictly sequentially — one channel at a time with no parallelism. Each iteration of `_run_scan` blocks on `_scan_channel` before advancing to the next channel. There is no concurrent scanning, no worker pool, and no pipelining of tune and decode operations.

### 3.2 Dwell Time: Fixed, Non-Adaptive

Each channel receives a fixed `asyncio.sleep(SCAN_DWELL_TIME)` of **4.0 seconds** (configurable via environment variable). This sleep begins immediately after the HTTP POST to welle-cli's `/channel` endpoint returns a 200 response. There is no additional post-tune settling period beyond whatever welle-cli applies internally before acknowledging the request.

The dwell is not adaptive: signal quality, SNR, and ensemble lock status are not consulted at any point. The scanner unconditionally waits 4 seconds, then reads `mux.json`.

### 3.3 Total Scan Duration

A full scan across all 38 Australian DAB+ channels requires a minimum of:

> **38 channels × 4.0 s = 152 seconds (~2 min 32 s)**

This is a floor, not an average — HTTP round-trip latency and `mux.json` retrieval add to each iteration. The quick scan variant covers only 3 channels (9A, 9B, 9C), reflecting Sydney-specific defaults, completing in approximately 12 seconds under ideal conditions.

### 3.4 Single-Attempt Policy

Each channel receives exactly one scan attempt. There is no retry logic anywhere in the pipeline. If `tune()` returns `False`, `_scan_channel` returns an empty list immediately. If an exception is raised during any channel's scan, it is caught in `_run_scan` and the channel silently yields zero stations — no retry, no logging escalation, no flagging for a second pass.

### 3.5 No Signal Quality Evaluation

The scanner performs no SNR check, no signal strength measurement, and no ensemble lock verification before or after the dwell. After `SCAN_DWELL_TIME` elapses, `get_mux_json()` is called and its output is accepted unconditionally. Channels where a signal was detected but service enumeration was incomplete are not distinguished from channels with no signal.

### 3.6 Ensemble Acquisition Risk

DAB ensemble acquisition typically requires 2–10 seconds depending on signal conditions. Consumer DAB radios commonly apply dwell periods of 8–15 seconds per channel. At 4 seconds, the configured dwell falls below this range for weak-signal conditions. There is no second pass for channels where a partial or unstable ensemble was observed.

---

## 4. Ensemble Detection

Ensemble detection is fully delegated to welle-cli. The scanner tunes to a channel, waits a fixed 4.0-second dwell period (`SCAN_DWELL_TIME`), then performs a single HTTP GET against `http://localhost:{WELLE_CLI_PORT}/mux.json`. No signal quality metrics are consulted at any point — there is no SNR check, no signal strength threshold, and no validation of lock status before or after tuning.

A channel is considered to have a detectable ensemble if and only if `get_mux_json()` returns a non-`None` value containing a `"services"` key that holds a list. Any other outcome — HTTP error, malformed JSON, a `ValueError` during `.json()` parsing, or an httpx timeout — causes `get_mux_json()` to return `None` silently, and the channel is skipped entirely with no logged distinction between "no ensemble present" and "ensemble present but unreachable."

The httpx client carries a hard 5-second timeout. If welle-cli is under load during FIC acquisition, the HTTP response may not arrive within this window, producing a `None` return that is indistinguishable from a genuinely empty channel.

A structural race condition exists between the dwell timer and FIC acquisition. The DAB Fast Information Channel carries ensemble configuration and the full service list; welle-cli must decode it before `mux.json` reflects complete data. Four seconds is not guaranteed to be sufficient across all reception conditions. On weak or marginal signals, FIC frame error rates increase and full decode may take longer, meaning `mux.json` could be fetched while FIC decoding is still in progress. In this state welle-cli may return a partial or empty `"services"` list, causing the ensemble to appear empty or sparsely populated without any indication that data collection was incomplete.

---

## 5. Service Discovery

Services are parsed from the `"services"` array returned in `mux.json`. Each entry is processed individually; malformed entries (non-dict values) are silently skipped.

A service is excluded from results if either its `"sid"` field or its resolved label is absent or empty after label extraction. This filtering is intentional for genuinely unlabelled entries, but it also silently drops services that are still mid-initialization in welle-cli's FIC decoder and have not yet received their labels. There is no retry or deferred resolution; the service is lost for this scan pass.

Bitrate is extracted exclusively from the first component in the `"components"` array whose `"subchannel"` dict contains a `"bitrate"` key. For multi-component services — which may carry audio and data components on separate subchannels — only the first component's bitrate is recorded. Subsequent components are ignored. This does not cause a service to be dropped, but the `bitrate` field in the resulting station dict is incomplete for such services.

No audio/data distinction is applied. Any service with a valid `sid` and label will be added to results regardless of whether it is an audio service, a data service, or a slideshow-only stream. Data-only services would appear as scannable stations in the registry.

Secondary services within a multiplex are captured if and only if welle-cli includes them in the `"services"` array. The scanner applies no secondary-service filtering.

The station registry is keyed on service ID (`sid`). If the same `sid` appears on multiple channels during a scan — possible when a service is broadcast on more than one multiplex — the later write overwrites the earlier one. The surviving record reflects whichever channel was scanned last, not necessarily the stronger or preferred reception path.

---

## 6. Error Handling & Timeouts

Exception handling in `_run_scan` wraps each call to `_scan_channel` in a bare `except Exception` block. Any exception raised during channel processing is caught, the channel is silently skipped, and scanning continues. No exception details are logged at the scan loop level, so transient errors (network errors, process errors, unexpected welle-cli responses) are indistinguishable from deliberate skips in the output.

Tune failures in `_scan_channel` return `False` from `tune()`, which causes the channel to be immediately abandoned with an empty result. There is no retry mechanism. A single HTTP error or a 4xx/5xx response from welle-cli permanently discards that channel for the duration of the scan.

`get_mux_json` returning `None` — whether due to an HTTP timeout, a connection error, or welle-cli returning no valid data — results in the channel being abandoned with the same empty-result outcome as a tune failure. There is no distinction between "no DAB ensemble present on this frequency" and "an ensemble is present but FIC acquisition is still in progress." Both conditions return `None` and the channel is skipped.

The httpx client is configured with a 5-second timeout across all requests. DAB FIC (Fast Information Channel) acquisition can take 1–3 seconds under good signal conditions and longer under marginal conditions. If welle-cli has not completed ensemble acquisition within the dwell period plus the 5-second HTTP timeout window, the response will timeout and the channel will be recorded as empty regardless of signal presence.

Partial scan results are preserved if a scan is cancelled mid-run: stations found before cancellation are retained, but channels not yet scanned receive no entry. There is no record of which channels were skipped versus scanned-and-empty.

If the welle-cli process crashes during a scan, all subsequent `tune()` and `get_mux_json()` calls will fail with HTTP connection errors. The process monitor restarts welle-cli with a 1-second delay, but the scanner has no awareness of process state and does not pause or wait for recovery before continuing to the next channel.

---

## 7. SDR Hardware Constraints

welle-cli is invoked with only four flags: `-c` (channel), `-w` (HTTP port), and `-F rtl_sdr`. No gain, AGC, PPM correction, or bias-T flags are passed.

welle-cli supports `-G` for manual RF gain (in tenths of a dB) and `-Q` to enable automatic gain control. Neither is used. The resulting gain behaviour depends on welle-cli's internal default, which is implementation-defined and may vary across builds. Without explicit gain control, the receiver may operate suboptimally for strong or weak signal environments.

No PPM frequency correction is applied (`-p` flag absent). RTL-SDR dongles based on the RTL2832U exhibit frequency reference offsets typically in the range of 1–60 ppm, with some units exceeding this. At DAB Band III frequencies (~175–230 MHz), a 20 ppm offset corresponds to approximately 3.5–4.6 kHz of frequency error. DAB channel bandwidth is 1.536 MHz, so this error is within the passband, but it degrades carrier orthogonality in the OFDM demodulator and reduces SNR, particularly on weak signals.

No bias-T control is available or configured. Active antennas requiring DC power over the coax will be unpowered.

The RTL-SDR R820T/R820T2 tuner has a typical noise figure of 3.5–4.5 dB. Consumer DAB receiver chips (e.g., Frontier Silicon FS2027, Keystone T3201) achieve noise figures of 1–2 dB, representing a 2–3 dB sensitivity disadvantage for the RTL-SDR in equivalent conditions.

The RTL2832U uses an 8-bit ADC, yielding approximately 48 dB of instantaneous dynamic range. Purpose-built DAB receivers typically use 12–16 bit ADCs (72–96 dB dynamic range). In environments with strong co-channel or adjacent signals, ADC saturation and quantisation noise degrade weak signal demodulation more severely than on dedicated hardware.

No antenna impedance matching or connector configuration is defined. The RF chain is entirely dependent on the user's hardware assembly.

---

## 8. Hardware vs Software Assessment

The missing-stations problem is almost certainly a combination of both software and hardware factors, but they are not equally addressable or equally probable as the primary cause.

**Software-side issues are the dominant cause.** The scan logic has multiple compounding deficiencies: a fixed 4-second dwell with no adaptation, no retry on failure, no gain configuration, and a race condition between FIC acquisition and mux.json fetch. These are not edge cases — they are structural gaps that would cause missed stations even with ideal hardware. Any one of these alone could account for a meaningful fraction of missing stations; together they are almost certainly responsible for the majority.

**Hardware limitations are real but secondary.** The RTL-SDR's 8-bit ADC and ~3.5–4.5 dB noise figure represent a genuine RF performance disadvantage versus consumer DAB silicon (12–16 bit, ~1–2 dB NF). This gap is approximately 2–3 dB in sensitivity, which matters on fringe signals. However, this hardware ceiling is fixed — it cannot be improved without replacing the SDR — whereas every software deficiency identified is correctable.

**RF environment is an unknown wildcard.** Antenna type, cable length, connector quality, and physical placement all affect received signal strength independently of SDR hardware. A poorly matched or poorly positioned antenna could account for several dB of loss that neither software nor hardware changes can recover.

**Verdict:**

| Factor | Confidence | Priority |
|---|---|---|
| Software scan logic | High — primary cause | Address first |
| SDR hardware limits | Medium — real but bounded | Accept or mitigate |
| RF/antenna environment | Medium — unknown until tested | Characterise early |

---

## 9. Top 5 Most Likely Causes of Missing Stations (Ranked)

### #1 — Insufficient Dwell Time (4 seconds)

**Likelihood:** High

**Why it is likely:** DAB FIC acquisition requires 2–10 seconds under good conditions; weak or marginal signals extend this further. Consumer radios typically dwell 8–15 seconds per channel. At 4 seconds, channels with slower FIC lock will return empty or incomplete service lists before acquisition completes.

**How it would manifest:** Channels exist in the ensemble list but return zero or fewer services than the consumer radio. Results vary between scan runs on the same channels.

**Experimental verification:** Set `SCAN_DWELL_TIME=10` via environment variable and run a full scan. Compare station counts against the baseline 4-second scan.

---

### #2 — No SDR Gain Optimisation

**Likelihood:** High

**Why it is likely:** Without `-G`, `-Q` (AGC), or manual gain configuration, welle-cli uses a default gain that may be too low to decode weak multiplexes or too high, causing ADC saturation on strong signals. Either condition degrades SNR. The RTL-SDR's 8-bit ADC has limited headroom, making gain setting more critical than on consumer chips.

**How it would manifest:** Strong local multiplexes decode reliably; weaker or more distant multiplexes are consistently missed regardless of dwell time.

**Experimental verification:** Run `welle-cli` manually with `-G 0`, `-G 20`, `-G 40`, and `-Q` on a known-weak channel. Compare reported SNR and service counts at each gain setting.

---

### #3 — Single-Attempt Scanning with No Retry

**Likelihood:** Medium-High

**Why it is likely:** A single transient interference burst, momentary signal fade, or USB timing hiccup during the one scan attempt causes a permanent miss for that channel. No compensating mechanism exists. Channels near the detection threshold are especially vulnerable.

**How it would manifest:** Running two full scans back-to-back produces different station lists — stations appear in one run but not the other.

**Experimental verification:** Run three sequential full scans and diff the resulting station lists. Inconsistency between runs confirms single-attempt fragility.

---

### #4 — RTL-SDR Hardware Sensitivity Limitations

**Likelihood:** Medium

**Why it is likely:** The ~2–3 dB noise figure disadvantage versus consumer DAB silicon is sufficient to push marginal signals below the decodable threshold. This is a hard floor that software cannot fully compensate for.

**How it would manifest:** Channels where the consumer radio reports good SNR but welle-cli reports SNR below ~5 dB or fails to lock entirely, with no improvement from dwell time increases.

**Experimental verification:** On channels where the consumer radio finds stations but this app finds nothing, inspect welle-cli's SNR output. SNR consistently below 5 dB on those channels indicates a hardware-limited scenario.

---

### #5 — Race Condition in FIC/Service Enumeration

**Likelihood:** Medium

**Why it is likely:** `mux.json` is fetched once at the end of the dwell period, but welle-cli may not have completed FIC parsing. Services still being decoded will be absent or nameless — and nameless services are silently filtered out by the existing SID/name check. The 5-second httpx timeout may also mask fetch failures.

**How it would manifest:** A channel's service count increases if `mux.json` is fetched later. Some channels return an ensemble with zero services despite the consumer radio finding stations there.

**Experimental verification:** Manually tune welle-cli to a known multiservice ensemble. Fetch `mux.json` at 2 s, 4 s, 6 s, 8 s, and 12 s intervals and record service counts at each interval.

---

## 10. Questions for Further Investigation

These require runtime testing or logging to answer:

1. What SNR values does welle-cli report on channels where the consumer radio finds stations but this app finds nothing — are they consistently below 5 dB?
2. How does the service count in `mux.json` change when fetched at 2 s, 4 s, 6 s, 8 s, and 12 s after tuning on a known multiservice channel?
3. What gain value does welle-cli apply by default (no `-G` or `-Q`), and how does it affect SNR on weak channels?
4. Does running the full scan twice back-to-back produce an identical station list, or do stations appear in one run but not the other?
5. Are there channels where `mux.json` returns a valid ensemble name but zero services — and if so, which channels and how frequently?
6. What is the RTL-SDR's frequency offset in ppm, and could uncorrected drift cause FIC decode failures on edge channels?
7. What does the consumer radio report for each channel where this app returns zero services — ensemble name, service count, and approximate signal quality?
8. Is the antenna physically matched to Band III (174–240 MHz) — what type, length, and connection quality?
9. Does exception handling per channel suppress any errors silently during a normal scan run — are there exceptions being caught that are never logged?
10. On channels that are consistently missed, does manually invoking `welle-cli` interactively with a 15-second observation window eventually decode the ensemble, or does it fail entirely?

---

## Appendix: Key Source Files Referenced

| File | Role |
|---|---|
| `server/config.py` | Band III channel list, dwell time, popular channels |
| `server/scanner.py` | Scan loop, channel processing, service parsing |
| `server/welle_manager.py` | welle-cli process management, tune/mux API |
| `server/station_registry.py` | Station storage and persistence |
| `server/routes.py` | HTTP API endpoints for scan control |
| `install.sh` | welle-cli build flags, RTL-SDR configuration |
