# Measured Device Evidence And Native Launch

## Implemented

`tools/collect_device.py` collects one whole observed device into a new local
directory. It uses an explicit executable, a loopback origin, two launches of
profile A and one of profile B. It does not download a browser, grant media
permissions, overwrite an existing bundle or install synthetic device records.

```powershell
python tools/collect_device.py --browser C:/verified/chrome.exe --output .chromix-local-build/device-a
python tools/device_pool.py validate .chromix-local-build/device-a/record.json
python tools/device_pool.py select --host .chromix-local-build/device-a/record.json --seed 0x100000001 .chromix-local-build/device-a/record.json
```

Headless is the default and is recorded in browser evidence. Use `--headed` to
observe a native desktop window; a headless display is not a measured physical
monitor. Never change the source record to make a candidate fit the host.

The collector measures:

- OS/release/build/architecture and logical CPU count. Windows adds CIM CPU,
  physical RAM and GPU driver inventory. Other platforms explicitly retain
  unavailable fields rather than borrowing Windows data.
- Font-file hashes from Windows system/user font directories, or `fc-list`.
  These are inventory hashes, not proof of which font supplied a glyph.
- UA/CH, locale/timezone, CPU count, deviceMemory, native heap exposure, actual
  Wasm/SIMD execution, bounded Wasm memory growth, Atomics, SAB and isolation in window, iframe,
  dedicated worker, shared worker and service worker.
- Screen/available bounds, actual outer/inner dimensions, window position,
  visualViewport, DPR and matching CSS device-size/resolution queries. Negative
  monitor coordinates and windows spanning displays are permitted.
- WebGL2 identity/limits/extensions plus shader/texture readback; WebGPU
  identity/features/limits, actual `requestDevice`, compute/map and texture readback;
  CSS/Canvas/Font Loading samples for Latin/CJK/emoji/math/missing characters, AudioContext properties,
  permission-filtered media enumeration/constraints, and network estimates.
- Cross-context identity/execution agreement, restart stability and independent
  profile LocalStorage isolation. IDs remain raw in evidence, but salted media
  IDs, echoed HTTP headers, network estimates and dynamic audio state/latency are excluded from the
  stable device snapshot. Media kinds/labels/constraints are retained.

## Record Format

`record.json` schema version 1 has exactly seven fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `kind` | `measured`; synthetic/test-only records are rejected |
| `record_id` | SHA-256 of canonical JSON of all other top-level fields |
| `provenance` | Collector ID, timezone-aware collection timestamp, executable SHA-256 |
| `device` | Whole host inventory and stable five-context surface observations |
| `evidence` | Relative `host.json` and `browser.json` paths and file SHA-256 values |
| `qualification` | Fixed unverified physical-backend and uncollected wire statuses |

Canonical JSON is sorted-key, ASCII-escaped, compact JSON with non-finite values
forbidden. Evidence paths are resolved inside the bundle; absolute paths,
traversal and escaping symlinks are rejected. Duplicate JSON keys, digest
mismatches, field splicing, missing contexts, observed API errors, inconsistent
identities, invalid dimensions and failed restart/isolation checks fail validation.
Raw evidence is preserved when record validation fails. A failed collection may
leave only `host.json` and `failure.json`; that is not a usable pool record.

Checksums do not authenticate hardware or prove a collector was honest. A
`measured` label is not a signed attestation. Keep raw device bundles local:
they include installed font paths, hardware inventory and origin-salted IDs.

## Selection Contract

The conservative v1 solver only selects among exact matches of host inventory,
correlated observations and executable hash. Missing native GPU, RAM, font or OS
inventory cannot qualify. Seed selection is a deterministic SHA-256 rendezvous
ranking over entire record IDs, stable across input order and using all uint64
seed bits. It has no invented market-share weights.

No matching record returns `status: native`, `record: null`, rejection reasons
and an empty override list. A match returns a copy of the entire record and an
empty override list. There is no per-field fallback or composition across records.

## SDK Launch Integration

Selection now gates Python sync/async context and persistent-context APIs and
Node context/persistent-context APIs. The validator and browser probe ship in the
Python package; `tools/device_pool.py` is a repository CLI wrapper around it.

```python
from chromix import launch_context, launch_persistent_context

pool = {
    "host": ".chromix-local-build/device-a/record.json",
    "records": [".chromix-local-build/device-a/record.json"],
    "seed": "4294967297",
}
context = launch_context(device_pool=pool)
assert context._chromix_device_profile["runtime_verified"]
context.close()
context = launch_persistent_context("./measured-profile", device_pool=pool)
context.close()
```

Set `CLOAKBROWSER_BINARY_PATH` to the exact collected executable first. Async
equivalents are `launch_context_async` and `launch_persistent_context_async`;
pass `user_data_dir` by keyword for the async persistent API.

```javascript
import { launchContext } from '@xiaoxiaofeihh/chromix';

const context = await launchContext({
  devicePool: {
    python: 'python',
    host: '.chromix-local-build/device-a/record.json',
    records: ['.chromix-local-build/device-a/record.json'],
    seed: '4294967297',
  },
});
console.log(context.chromixDeviceProfile.runtime_verified);
await context.close();
```

Node uses the same packaged Python validator over JSON pipes without a shell.
Install this checkout's Python SDK (`python -m pip install ./sdk/python`) into
the interpreter selected by `devicePool.python`. It does not silently install
Python or resolve repository-relative imports. Node still needs its usual
Playwright dependency. Use strings or BigInt for seeds above the JS safe-integer
range.

The launch contract is deliberately restrictive:

- Revalidate evidence files, current host inventory, exact executable hash and
  headed/headless mode before launch. Host evidence expires after 24 hours;
  `max_age_hours` accepts `(0, 168]`. Future timestamps are rejected.
- Only `headless`, browser version/release selection and the persistent directory
  may accompany measured mode. Proxy, args, viewport, UA, locale, fonts and other
  overrides are rejected, including nested Node options. Browser-returning
  `launch` APIs reject measured mode because they cannot verify future contexts.
- Keep native display, CPU, V8, GPU, fonts, audio and media behavior. Launch uses
  `--fingerprint=off`, real WebGL, noise disabled and no viewport emulation.
  No different physical GPU or virtual device is created by selecting a record.
- Collect all five live contexts before returning the context. A capability,
  identity, screen or DPR mismatch closes the context and browser. Window resize
  is allowed, but dimensions and CSS screen/resolution self-consistency must pass.
- Persistent profiles atomically bind the selected record, executable and uint64
  seed in `.chromix-device-profile.json`. Changed bindings fail rather than rotate.
  The binding remains after a failed launch. Recollection, pool changes or moving
  between native fallback and a selected record may require a new profile.
- No match verifies the current native host record instead of mixing fields from
  rejected candidates. Runtime verification covers this startup, not all future
  permission, display, driver or browser-context changes.

Measured mode remains native (`--fingerprint=off`) and rejects per-field
overrides. Outside measured mode, public fingerprint launches now supply fixed
CPU/RAM and platform screen/taskbar/quota defaults, plus platform GPU identity
templates; explicit public CPU/RAM/GPU identity flags do not require synthetic
opt-in. These defaults are **not measured device records**. See the
[public flag contract](fingerprint-flags.md).

The older independent seeded CPU/RAM/display and GL-capability templates still
require `--uxr-synthetic-device-tests=true`, as do legacy font whitelist/
substitution and persona fallback. SDK default page viewport is native unless
explicitly configured. Public GPU identity flags preserve actual GL capabilities;
WebGPU feature filters intersect real support and Dawn owns actual limits. The
heap getter retains V8's native limit rather than deriving it from UA bitness.
These changes require a rebuilt browser, not reuse of an older binary.

## P0 Capability Audit

```powershell
python -X utf8 tools/device_p0_audit.py --browser C:/verified/chrome.exe --output .chromix-local-build/device-pool-p0/audit.json
```

Probe v2 executes Wasm scalar/SIMD, memory growth and maximum enforcement,
WebGL shader rendering and extension requests, WebGPU compute and mapped texture
readback. Validators check the returned results, not just success labels.
The standalone audit runs five contexts on nonisolated and COOP/COEP-isolated
origins, and verifies an actual SAB/Atomics roundtrip through a dedicated worker.
SharedWorker isolation can differ from its creator; native context semantics are
preserved instead of requiring every worker to expose SAB.

Each scope fetches a loopback HTTP header echo. UA, Accept-Language and negotiated
CH are compared to JS in live startup and raw record validation. Worker fetches
may omit CH; omitted worker hints are not fabricated, but any present hint must
match. Window/iframe require the negotiated hints. These are local HTTP identity
checks, not proxy or encrypted-protocol verification.

The audit adds CDP platform-font family/PostScript/glyph-count evidence for the
20 font samples. This is not a font-file binding or a Canvas rasterization proof.
`passed` means the tested operations passed; unavailable GPU APIs remain explicit,
and `wire`, `physical_gpu_equivalence`, `font_file_binding` remain unverified.
No maximum JS heap allocation, full shader/format/limit matrix or driver identity
attestation is performed. Recollect pre-v2 bundles before using the updated live
probe; their stable projection no longer matches. Existing profile bindings do
not silently migrate to the new record.

## Wire Evidence

```powershell
python tools/device_wire_evidence.py --pcap captures/session.pcapng --keylog captures/session.keys --output captures/wire.json
```

This is offline parsing of an existing capture via installed `tshark`; it never
starts capture or traffic. Output contains the capture hash and decoded frame
numbers and decoded address/port, TLS, HTTP/2 settings/headers, QUIC version, DNS,
HTTP CONNECT and STUN fields. Key logs are
optional and are not copied into the report. The frame cap and possible truncation
are reported. Missing tools and parse errors fail instead of producing a pass.

Protocol presence is not route, TLS-profile, QUIC-parameter or ICE/TURN acceptance.
No packets is `not_observed`, never `unsupported`. Reports deliberately cannot be
imported as verified capability records. Process attribution, TLS/HTTP2 expected
fingerprint comparison and capture-to-device binding remain to be implemented.

Optional `--route-policy captures/route.json` checks captured outbound TCP/UDP
endpoints against exact rules, including IPv6. Example policy:

```json
{"client_ips":["192.0.2.2"],"allowed_egress":[{"protocol":"tcp","ip":"192.0.2.3","port":8080}]}
```

Mismatch exits nonzero. No outgoing packets, ambiguous encapsulation, or a
possibly truncated matching capture is inconclusive and also exits nonzero.
`observed_match` only covers captured source-IP traffic; `route_verified` stays
false. It cannot prove that another interface/process or uncaptured DNS, QUIC or
WebRTC traffic did not bypass the configured route.

## Remaining Work

### Corpus review tooling (2026-09-12)

New collections include `browser_versions` for all three launches and the exact
device-probe SHA-256 in browser evidence; the record provenance repeats the full
browser version and probe hash. Older bundles remain readable by the v1 selector,
but must be recollected for corpus review. This does not change the selector's
exact-native matching or its existing 24-hour live-preflight age default.

`tools/fingerprint_corpus_review.py` checks an explicitly prepared review manifest:

```powershell
python -X utf8 tools/fingerprint_corpus_review.py corpus/review.json --output C:/diagnostics/corpus-new.json
```

Manifest template (placeholders are not executable evidence or a bundled dataset):

```json
{
  "schema_version": 1,
  "browser_version": "152.0.7977.82",
  "probe_sha256": "<SHA-256 of sdk/python/chromix/device_probe.js>",
  "max_age_days": 30,
  "cohorts": [{"id": "windows-amd64", "os": "Windows", "architecture": "AMD64", "min_devices": 2}],
  "samples": [{
    "path": "machine-a/record.json",
    "record_id": "<measured record_id>",
    "record_sha256": "<SHA-256 of that record.json>",
    "device_id": "<internal pseudonymous device tag>",
    "cohort_id": "windows-amd64",
    "reviewer": "<internal reviewer tag>",
    "reviewed_at": "<ISO-8601 timestamp with timezone>",
    "expires_at": "<ISO-8601 timestamp with timezone>",
    "sample_kind": "physical"
  }]
}
```

The template deliberately needs real records and enough distinct devices before
it can pass. Review checks bundle checksums, all five contexts, native inventory,
full browser/probe version, collection age, review/expiry ordering, OS/architecture
cohort membership, duplicate records and repeated device tags. `virtual`, `control`
and `fixture` samples are retained as excluded diagnostics, never counted toward
physical-device cohort minima. The tool does not modify records or selector state,
manufacture reviewer approvals, or turn sample counts into population weights.

`passed` means these integrity/metadata checks passed. Reviewer/device tags are
supplied metadata, not authenticated proof. Physical backend equivalence, external
routes and font-file-to-glyph binding remain false in the output. Native font-file
inventory hashes and CDP family/PostScript/glyph counts are still separate evidence;
a matching name alone cannot prove the exact file that rasterized a glyph.

Canvas now has a separate [chain audit](canvas-chain.md) for pixels, codecs,
color spaces, alpha, ImageBitmap and five-context/restart comparisons. The local
stock-browser run fails; it is not a pool qualification or matching-build pass.
Default legacy Canvas noise and bridge substitutions require synthetic-test
opt-in, consistent with the native-capability device-pool policy.

- Collect reviewed, provenance-backed hardware samples. No real pool is bundled.
- Implement backend binding if devices other than the exact native host are to
  be supported. The current SDK mode validates native execution, not emulation.
- Validate zoom/orientation/multiple displays and maximum JS heap allocation;
  execute the isolated-context audit on matching patched binaries.
- Bind glyph provenance, actual rendering/decoding/capture and permission changes.
- Add IndexedDB/Cache isolation and remote encrypted HTTP identity comparisons;
  the collector now checks negotiated local HTTP CH against JS.
- Run against an executable built from the matching Chromix patch series. A stock
  Chrome collector test is not patched Chromium integration acceptance.

## Verification

```powershell
python -X utf8 -m pytest tools/tests/test_device_pool.py tools/tests/test_device_launch.py sdk/python/tests/test_api.py -q
node --test sdk/node/test/api.test.mjs sdk/node/test/device_pool.test.mjs
node --check sdk/python/chromix/device_probe.js
```

Use Python UTF-8 mode on Windows: the pre-existing smoke tests read UTF-8 reports
using the process default encoding, which otherwise can be GBK.

The collector run on 2026-09-10 against installed stock Chrome 153.0.8010.37 completed all
five contexts and three launches; the resulting bundle passed validation and
exact-match selection. It observed 32 logical cores and a 16 GiB deviceMemory
bucket. That private headless control bundle is not a reviewed physical-device
pool sample and is excluded from git. No native Chromix binary or real packet
capture was validated in this run. Python sync/async context launch and async
persistent restart, plus Node context and persistent restart, passed live startup
verification using that stock executable. These are SDK workflow tests only.

Historical P0 targeted regression (before the 2026-09-12 changes): 1102 Python tests passed, 23 skipped, and 302
font subtests passed; Node SDK: 142 passed, 2 skipped. Skips cover platform or
unconfigured source/browser prerequisites. The 124-patch linter and JS syntax
check passed. Standalone C++ probes cover explicit-override gating and native
font fallback; the 0047/0118 patch chain applies and reverses without fuzz/offset.
The updated audit passed both isolation modes on stock Chrome 153.0.8010.37,
and the recollected v2 bundle passed validation. Stock results verify the harness,
not that this patch series has passed native Chromium acceptance.
An additional GPU consistency run could not link its existing UBSan seed probe
with this Windows toolchain (MSVC runtime mismatch/unresolved sanitizer symbols).
That broader suite is not counted as passing and needs a compatible sanitizer
toolchain; no sanitizer or unrelated test was disabled to produce a green result.
