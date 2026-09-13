# Building Chromix for Windows x64, Linux x64/arm64, and macOS x64/arm64

The build scripts target Windows x64, Linux x64/arm64, and macOS x64/arm64 using
pinned `ungoogled-chromium` sources. Each successful platform is verified and
published independently to its Chromium version's release tag; other platforms
append as they succeed, without a five-platform or shared-source-SHA gate.

Every platform uses this source-layer order:
**Chromium archive → ungoogled core patches → matching platform patches →
binary pruning → Chromix `patches/series`**. The platform layer is
`ungoogled-chromium-windows`, `ungoogled-chromium-portablelinux`, or
`ungoogled-chromium-macos`, respectively. Architecture selects native toolchains
and GN arguments, not a different official Chromium checkout.

## Pinned source layers

| Layer | Version | Commit |
|---|---|---|
| Chromium | `152.0.7977.82` | source archive selected by ungoogled-chromium |
| ungoogled-chromium | `152.0.7977.82-1` | `e71b91c6e336d0f25cfc6b9ef09298a9d2506e24` |
| ungoogled-chromium-windows | `152.0.7977.82-1.1` | `333bc7dfff72ff4abc4d9cc76bc41de300a46e06` |
| ungoogled-chromium-portablelinux | `152.0.7977.82-1` | `02c59ed68d1963a647bb478064823d114e466ffb` |
| ungoogled-chromium-macos | `152.0.7977.82-1.1` | `038db2b41f7aeb00bbceb2f5a56912b26eb5b284` |
| Chromix patches | `patches/series` | content hash stored in source markers |

The machine-readable pins are in `build/ungoogled-revisions.psd1`; the legacy
version files remain available for tooling compatibility.

## Linux x64/arm64 builds

The Linux ARM64 workflow builds on an x64 Ubuntu 24.04 host, matching the
pinned portablelinux donor's cross-build environment. It restores ARM64 target
sources and objects while executing x64 LLVM, Rust, GN, Node, Go, and Ninja.
Both amd64 host and arm64 target sysroots are checked. Their directory metadata,
stamp contents, and first-class download markers are recorded before installation;
observed replacement invalidates compiled outputs, including on resumed stages.
Target receipt identity,
GN arguments, patch application, and artifact names remain ARM64. This avoids
replacing a usable donor compiler solely because its host CPU differs from the
target CPU; genuine tool or dependency incompatibility still invalidates outputs.

The cross-build stage checks the ZIP checksum and every packaged ELF file's
AArch64 architecture, then reports compilation complete with runtime verification
pending. A required `ubuntu-24.04-arm` job downloads that exact same-run ZIP,
checks the checksum again, and executes its extracted launcher for version and
headless DOM smoke tests. The workflow and release gate cannot succeed if this
native verification fails. A successful static ELF check is not a runtime test.
Explicit cold builds (`use_upstream_cache=false`) retain the native ARM64 runner;
the cross-host route requires a verified full restored tree.

Standalone native builds remain supported. Prerequisites are a native
Debian/Ubuntu build host with Chromium's Linux build
packages, Python 3, Git, Ninja, Node.js, Go, `zip`, `unzip`, and GNU `sha256sum`.
Budget at least 100 GB free as a starting estimate, not a sufficient-space
guarantee: archives, toolchains, objects, staging, ZIPs, and smoke extraction
coexist. Native Linux arm64 additionally builds LLVM and Rust from source; the
CI cross-build instead uses the restored x64-host tools. Run:

```bash
build/build.sh /path/to/chromix-linux-build x64
build/linux/package-linux.sh /path/to/chromix-linux-build/src/out/Chromix /path/to/dist x64
```

Use `arm64` on a native Linux arm64 host:

```bash
build/build.sh /path/to/chromix-linux-arm64-build arm64
build/linux/package-linux.sh /path/to/chromix-linux-arm64-build/src/out/Chromix /path/to/dist arm64
```

The package names are `chromix-linux-x64.zip` and `chromix-linux-arm64.zip`,
with a launcher, fonts, and Chromium/Chromix license files. These are runtime
bundles, not fully static binaries; the target host still needs compatible
system libraries and a working Chromium sandbox.

The pinned portablelinux ARM64 patch has an incorrect Rust import hunk count
(`7/7` instead of `8/8`). GNU patch can skip the following four Rust hunks,
leaving x86_64 host-tool assumptions in place. Linux preparation corrects this
specific patch header before applying the platform layer; the intended source
changes and layer order stay unchanged. Prepared trees created with the old
preparation hash require a clean work directory.

## Native macOS builds

Use Xcode 26 or newer with a macOS 26 SDK or newer, the command-line tools,
Python 3, Git, Ninja, Node.js, Go, `zip`, `unzip`, and `shasum`. Although Chromium
152's GN configuration still declares `mac_sdk_min = "15"`, `launch_mac.cc`
references an API declared only by SDK 26; the runtime availability guard does
not make that declaration available when compiling against an older SDK.
Budget at least 100 GB free as a starting estimate, with the same caveat about
simultaneous source, toolchain, object, staging, ZIP, and smoke-test storage.
The architecture must match the runner or host:

```bash
build/macos/build.sh /path/to/chromix-mac-build arm64
build/macos/package-macos.sh \
  /path/to/chromix-mac-build/src/out/Chromix/Chromium.app \
  /path/to/dist arm64
```

Use `x64` for Intel Macs. The package names are `chromix-mac-arm64.zip` and
`chromix-mac-x64.zip`. The workflow does not perform Developer ID signing or
notarization; these are unsigned distribution artifacts (any compiler-generated
ad-hoc signatures are not a distribution signature). Gatekeeper may block a
locally downloaded bundle. No Apple credentials or notarization step are used.

### Chromium 152 Xcode/SDK compatibility

The local verification source at `.chromix-build-verify/src` identifies itself
as `152.0.7977.82` in `chrome/VERSION`. Inspection of that tree shows:

- `build/config/mac/mac_sdk_overrides.gni`: minimum macOS SDK **15**.
- `build/config/mac/mac_sdk.gni`: upstream reference SDK **26.5**, build
  **25F70**; runtime/deployment minimum **13.0** is not the build SDK minimum.
- `build/mac_toolchain.py`: upstream hermetic package records Xcode **26.6
  (17F113)** and SDK 26.5. This is a reference toolchain, not a claim that it is
  installed on either GitHub runner.
- `build/config/c++/modules.gni`: autogenerated Apple modules require Xcode
  **26+**; system Xcode defaults to manual modules. SDK 15 alone does not prove
  that other GN/toolchain paths or APIs will build successfully.

Before source preparation, `build/macos/select-xcode.sh` preserves a supported
`DEVELOPER_DIR` or active Xcode, otherwise searches installed Xcode applications
for an actual macOS SDK >=26. It records the Xcode version and SDK path and
exports the selected `DEVELOPER_DIR` through `GITHUB_ENV` for every CI stage.
The native macOS build invokes the same helper. Missing SDK 26 fails early.
On GitHub-hosted macOS runners, each stage then runs
`build/macos/free-disk-space.py` before downloading a handoff or upstream tree.
It preserves the selected Xcode, macOS SDK, runner/work directories, and tool
paths while removing only confined unused Xcode bundles, then mobile SDKs and
simulator runtimes if necessary. Cleanup stops at a best-effort 120 GiB target or
when its allowlist is exhausted. That target is not an admission requirement:
run `34261688438` reclaimed space from 42.35 to 78.49 GiB, and the old arbitrary
120 GiB gate rejected it before examining an archive. The fetcher instead checks
space for the actual archive sizes and retains 4 GiB headroom during extraction;
required-restoration mode fails on a real space rejection, without cold fallback.
Before/after space and each deletion are recorded in `disk-cleanup.log`. Neither
cleanup nor successful extraction proves capacity for a subsequent link or package.

The September 2026 `macos-15` and `macos-15-intel` runner inventories include
Xcode 26 while their default Xcode 16.4 supplies SDK 15.5. The pinned upstream
macOS build explicitly selects Xcode 26 as well. Choosing the installed SDK
addresses the observed `posix_spawn_file_actions_addchdir` compile failure;
a complete Chromium build remains necessary to establish compatibility with
other APIs. This workflow neither downloads a private Apple toolchain nor
changes SDK pins/GN requirements to hide a mismatch.

## GitHub Actions cross-platform build

Five independent workflows each own one platform's jobs and ZIP artifact.
The four POSIX entrypoints call `build-posix-github.yml`; Windows uses its
existing staged workflow directly. Failures and retries stay within that
platform, and no entrypoint cancels an active build.

| Workflow | Runner | Target | Archive |
|---|---|---|---|
| `build-linux-x64.yml` | `ubuntu-22.04` | Linux x64 | `chromix-linux-x64.zip` |
| `build-linux-arm64.yml` | `ubuntu-24.04` build; `ubuntu-24.04-arm` smoke | Linux arm64 | `chromix-linux-arm64.zip` |
| `build-macos-x64.yml` | `macos-15-intel` | macOS x64 | `chromix-mac-x64.zip` |
| `build-macos-arm64.yml` | `macos-15` | macOS arm64 | `chromix-mac-arm64.zip` |
| `build-win-x64-github.yml` | `windows-2022` | Windows x64 | `chromix-win-x64.zip` |

Each entrypoint supports manual dispatch with `use_upstream_cache=true` by
default. Filtered pushes start the affected platform workflows; shared build
changes can start all five. To push a repair without duplicating platforms
already running, include `[skip ci]` in the commit message, then dispatch only
the repaired platform. Confirm the source SHA and workflow before dispatching
and reuse an existing run rather than dispatching the same pair twice:

```bash
gh run list --workflow build-win-x64-github.yml --commit "$(git rev-parse HEAD)"
gh workflow run build-win-x64-github.yml --ref main -f use_upstream_cache=true
```

GitHub skips push-triggered workflows for `[skip ci]`, but still permits manual
dispatch. `build-posix-github.yml` is callable only through a platform entrypoint;
its platform and architecture must be supplied explicitly.

### macOS runtime failure recovery

Mac stages reserve 90 minutes before their stage deadline for packaging,
runtime checks and checkpoint handling, in both `staged` and `single` modes.
This includes the bounded version/headless checks, fingerprint dependency
installation and fingerprint acceptance gate. Linux reserves are unchanged;
runner disk speed, upload speed and job cancellation can still prevent recovery.

Once a Mac ZIP passes checksum verification, extraction, launcher, smoke and
fingerprint failures keep the job failed. Before packing a recovery checkpoint,
Actions uploads a separate `<artifact>-failed-runtime-sN-attempt-M` diagnostic
artifact containing the ZIP, `SHA256SUMS`, available runtime/fingerprint reports
and the stage log. It is not a verified release bundle. The smoke helper records
stdout, stderr, exit status and timeouts; on macOS timeouts it also attempts
bounded process and stack sampling. Successful completion still requires the
fingerprint gate.

Recovery snapshots retain the compiled tree but exclude root-level `dist`,
`smoke` and `runtime-smoke-stage-*` directories, so restored runs cannot reuse
stale smoke evidence. Select all checkpoint artifact IDs and their exact producer
attempt when resuming. Donors must come from `main`, or from a manual run on the
same explicitly selected recovery branch. Existing runs keep their original
workflow revision and patch set.

### Five-platform parallelism and incremental builds

All five Actions entrypoints accept `compile_jobs=auto|N`. The default `auto`
uses host CPU count and available RAM, reserving 2 GiB and budgeting 2.5 GiB per
compile. It is a conservative starting heuristic, not a peak-memory guarantee:
large translation units and linkers may need more memory. An unavailable memory
probe falls back to at most four jobs. Explicit values 1 through 1024 override
the heuristic; use them only after checking runner resources and swap activity.
Linux ARM64 cross-build parallelism is based on the x64 host, not target CPUs.
Each stage records its decision in the Actions summary. Windows no longer fixes
every runner to four compile jobs; POSIX no longer blindly uses every CPU when
memory is limited. Small hosted runners may still select four or fewer jobs.

The GN merge helper preserves `args.gn` bytes and mtime when its generated
contents are unchanged, including in-place merges after a snapshot restore.
This avoids unnecessary timestamp invalidation; GN generation still runs and
changed flags still invalidate affected build actions. No objects, depfiles or
Ninja command hashes are forged. A changed public header can still cause a
large rebuild.

Windows Actions additionally accept `build_profile=native|fast|release`.
`native` is the default and keeps the previous Windows GN policy. `fast` and
`release` explicitly disable/enable `thin_lto_enable_optimizations`, as on POSIX.
They do not disable CFI, the sandbox, codecs or GPU features. Profile changes
can themselves invalidate cached work; keep the donor's configuration for a
first resume, then benchmark a separate fast build if link time dominates.
The four POSIX defaults remain `fast` and `staged`.

### Five-platform handoff optimization (2026-09-11)

All five platforms record the snapshot producer attempt in a step output.
A same-run successor selects `needs.<previous-job>.outputs.snapshot_attempt`,
not a wildcard or the retrying consumer's current attempt. This preserves the
producer identity when only failed jobs are rerun. Cross-run resumes keep their
explicit `resume_attempt` selection. Missing artifacts remain an error; there is
no fallback that merges attempts.

Linux x64/ARM64 and macOS x64/ARM64 share the streaming packer:
`tar --format=pax | zstd -T0 -3 -c | split`. It writes temporary volumes directly,
rather than writing a complete compressed archive and copying it into volumes.
This removes approximately one compressed-archive-sized temporary allocation and
one compressed-archive read/write pass during slicing. Compression level, volume
limits, tar metadata and upload format are unchanged. Pipeline failure removes
temporary volumes before any upload slots are published; split is not retried
against an already consumed input stream. GNU `gsplit` selection on macOS is
retained.

These changes do not increase compiler parallelism, change GN profiles, weaken
cache identity checks, or claim a measured end-to-end speedup. Validate runner
timings and bundle smoke checks on the next native runs. Existing runs continue
to use the workflow revision with which they started.

Local validation for this change: Windows focused tests passed (85 tests,
97 subtests; 20 environment-specific skips) with `PYTHONUTF8=1`. WSL Debian
POSIX tests passed (129 tests, 1,239 subtests; 15 skips), including real GNU/BSD
tar, zstd and Ninja round trips and compressor/split failure injection. Bash 3.2
and unavailable native tooling tests remain skipped. `actionlint` passed all five
entrypoints and the reusable workflow with external shellcheck/pyflakes disabled.
No full Chromium build or new cloud workflow was dispatched in this pass.

### Resume the supplied Windows snapshot

At the 2026-09-10 metadata check, run `34080799322` is successful and its source
commit `23fd0a7a0c63cd452cfaec6b2aba8469ef5d4123` pins Chromium `152.0.7977.82`.
Its stage-8 attempt-1 snapshot consists of **both** artifacts:

| ID | Name | Bytes |
|---|---|---|
| `10066122665` | `tree-s8-attempt-1-part1` | 9663676664 |
| `10066143002` | `tree-s8-attempt-1-part2` | 2740944316 |

The linked artifact is only part 1, not a standalone build tree. The snapshot
expires around `2026-09-11T16:28Z`; recheck availability before dispatch. The
archive has not been downloaded or fully inspected in this optimization pass.
Matching version metadata is not proof that its old patch markers can migrate
to the current patch series; existing restore/preparation checks remain required.

After these workflow changes are pushed to the selected ref, use:

```bash
gh workflow run build-win-x64-github.yml --ref main \
  -f resume_run_id=34080799322 -f resume_tree_stage=8 \
  -f resume_attempt=1 -f resume_stage=2 \
  -f use_upstream_cache=false -f compile_jobs=auto -f build_profile=native
```

Stage 2 here means the first active job in the new run; `resume_tree_stage=8`
selects the donor snapshot, leaving stages 3-12 available for continuation.
The download pattern now selects one exact attempt instead of merging arbitrary
attempts with identically named archive volumes. `use_upstream_cache=false`
allows a Chromix snapshot without an upstream receipt and avoids requesting the
expired upstream donor. It does not skip receipt validation when one is present,
7-Zip integrity checks, patch preparation or compiler compatibility checks.
An incompatible legacy snapshot can still fail or require rebuilding objects;
do not remove those checks to claim cache reuse.

For a POSIX platform, retain its own architecture-specific donor and use e.g.:

```bash
gh workflow run build-linux-x64.yml --ref main \
  -f compile_jobs=auto -f build_profile=fast -f build_mode=staged \
  -f use_upstream_cache=true
```

The same inputs apply to `build-linux-arm64.yml`, `build-macos-x64.yml` and
`build-macos-arm64.yml`. The Windows snapshot is not portable to those targets.

### Resume a POSIX checkpoint on current code

All Mac and Linux entrypoints additionally accept `resume_run_id`, `resume_tree_stage` (1–8),
`resume_attempt`, and `resume_artifact_ids` (the complete recorded artifact ID
set). Select the exact attempt that **uploaded** all parts, not
necessarily the latest run attempt. For example:

```bash
gh workflow run build-macos-x64.yml --ref main \
  -f resume_run_id=34329646617 -f resume_tree_stage=7 -f resume_attempt=1 \
  -f resume_artifact_ids=10164699620 \
  -f use_upstream_cache=true -f build_profile=fast -f build_mode=staged -f compile_jobs=auto

gh workflow run build-macos-arm64.yml --ref main \
  -f resume_run_id=34329648981 -f resume_tree_stage=7 -f resume_attempt=1 \
  -f resume_artifact_ids=10162582694,10162583992 \
  -f use_upstream_cache=true -f build_profile=fast -f build_mode=staged -f compile_jobs=auto

gh workflow run build-linux-x64.yml --ref main \
  -f resume_run_id=34603635897 -f resume_tree_stage=1 -f resume_attempt=1 \
  -f resume_artifact_ids=10275990489 \
  -f use_upstream_cache=true -f build_profile=fast -f build_mode=staged -f compile_jobs=auto

gh workflow run build-linux-arm64.yml --ref main \
  -f resume_run_id=34603635882 -f resume_tree_stage=2 -f resume_attempt=1 \
  -f resume_artifact_ids=10284610558 \
  -f use_upstream_cache=true -f build_profile=fast -f build_mode=staged -f compile_jobs=auto
```

These historical snapshots expire; availability is checked before download.
For a build-only repair, push the fix with `[skip ci]` and manually dispatch only
the failed architecture. Manual builds at such a commit do not trigger automatic
Release reconciliation; publishing still requires a separate explicit release
workflow dispatch. This keeps existing assets and unrelated builds untouched.

The validator requires the same repository, architecture-specific workflow, `main`,
a terminal donor run, verified checkpoint and successful upload steps, and an
unexpired, contiguous artifact set matching the complete recorded IDs from that
exact attempt. Record every part ID when the snapshot is uploaded; listing only
surviving artifacts cannot prove the original set is complete. Each selected
artifact must include GitHub's SHA-256 digest. The downloader verifies its byte
count and SHA-256 before extracting ZIP members, checks ZIP CRCs and contiguous
volume names, and publishes the restore directory only after every artifact
passes. Interrupted transfers use bounded retries with fresh temporary files;
existing restore directories are never overwritten. Download and volume hashes
are recorded in `chromix-logs/snapshot-download.json`. The concatenated zstd
stream is decompressed and extracted into a temporary sibling tree. Both zstd
and tar must exit successfully before that tree replaces the build directory;
failed extraction leaves the prior tree, pinned download cache, and input
volumes intact. A failure stops the build rather than falling back to a cold build.
This detects incomplete compressed streams and truncated tar members, but cannot
reconstruct missing bytes in an already corrupt donor; select an intact checkpoint
or create a new one. A `premature end` / `Truncated tar archive` log alone does
not establish whether the corruption originated during packing or transfer.

The selected checkpoint starts at stage 1 of the new run, leaving all eight jobs
available. Current trusted tooling validates the old source-ready key and patch
output manifest, checks unchanged upstream pins/tooling/lite payload, reverses the
old patches in a temporary tree, and applies the current series there. Only net
source changes are published; unchanged source timestamps and Ninja/object state
are retained. Interrupted or incompatible migrations fail closed. Old repository
scripts are not executed. Ninja still recompiles dependencies affected by changed
headers, sources or build flags; a newer commit does not imply all old objects are
reusable.

No new Actions runs are dispatched by the optimization scripts or tests, and no
wall-clock speedup is claimed until native builds are timed.

### POSIX profile details

The four POSIX entrypoints accept `build_profile=fast|release` and
`build_mode=staged|single`. Pushes and manual dispatches default to `fast` with
`staged` recovery. Windows retains its existing configuration.

| Setting | Behavior |
|---|---|
| `fast` | Sets only `thin_lto_enable_optimizations=false`, reducing the main browser's expensive ThinLTO link optimizations. |
| `release` | Sets `thin_lto_enable_optimizations=true` for the original optimized release behavior. |
| `staged` | Allows up to eight jobs, handing off a snapshot when the current budget is exhausted. |
| `single` | Allows one compile job, reserves 15 rather than 45 minutes, and saves an unfinished checkpoint before failing if it cannot finish. Linux ARM64 still requires the separate native verification job. |

ThinLTO itself, its incremental cache, official non-component builds, sandboxing,
and browser features are unchanged. Symbols and PGO remain disabled as before.
The fast profile trades link optimization for build time; runtime performance and
binary size may differ, and no Chromium timing or runtime benchmark has yet
quantified that trade-off. Switching profiles forces affected links to rerun and
may rebuild dependent outputs; keep the profile consistent across a build.
Locally, POSIX builders default to `release`; select fast explicitly:

```bash
CHROMIX_BUILD_PROFILE=fast build/build.sh /path/to/chromix-linux-build x64
CHROMIX_BUILD_PROFILE=fast build/macos/build.sh /path/to/chromix-mac-build arm64
```

After publishing the workflow changes, a single-platform manual dispatch can use:

```bash
gh workflow run build-linux-x64.yml --ref main \
  -f build_profile=fast -f build_mode=staged -f use_upstream_cache=true
```

Use `single` only when a representative build fits the hosted job budget or a
failed time-boxed experiment is acceptable. It increases available compilation
time by reducing the handoff reserve, not by increasing CPU resources. An
unfinished build still saves a checkpoint before failing at its stage limit.
Existing active runs continue
using their original source revision and are unaffected by these settings.

The speed work borrows Camoufox's independent parallel targets, reduced costly
build options, and optional one-job builds rather than copying Firefox's `mach`
commands or Rust job limits into Chromium. Linux ARM64 already cross-compiles on
x64 when restoring upstream builds. Linux-to-Windows and Linux-to-macOS paths are
not implemented: they need reproducible platform SDK/tool distribution and native
runtime checks before replacing the current builders. A two-hour Camoufox build
is not evidence that Chromium can finish in the same time.

Two fixes reduce unnecessary work during staged recovery: POSIX pax snapshots
preserve nanosecond timestamps, and environment compatibility ignores scheduling
fields `RUNNER_NAME`, `GITHUB_JOB`, `GITHUB_RUN_ID`, and `GITHUB_RUN_ATTEMPT`.
These fields remain in diagnostic reports. Mac SDKs additionally record a full
content fingerprint, including files, modes and internal symbolic links. Only
matching complete fingerprints on both sides allow `ImageVersion` and SDK-root
size/mtime changes to be ignored; SDK path/version/settings and all other build
inputs remain checked. Image/root metadata drift still rechecks dependencies
outside the verified SDK roots; the SDK proof never exempts arbitrary external
headers or Xcode toolchain paths. SDK aliases require matching recorded root
bindings, and unresolved inputs remain unproven. Missing legacy fingerprints,
unreadable trees, external or broken links, and actual content changes
conservatively recheck dependencies.
An old ARM64 checkpoint can therefore require a one-time SDK-dependent rebuild;
matching Xcode version strings alone are not evidence of identical headers.
Cached Mac jobs preflight the actual SDK on every runner before downloading a
large checkpoint, record scan time/counts/digest, and stop if the fingerprint is
incomplete rather than repeatedly rebuilding with an unverifiable SDK. Untracked
compiler environment overrides such as `C_INCLUDE_PATH`, `OBJC_INCLUDE_PATH`,
and `CCC_OVERRIDE_OPTIONS` are rejected in this cached CI path before compilation.
Actual compiler, generator, sysroot, and dependency changes still invalidate
affected outputs. Node.js
`24.20.0` and Go `1.27.1` are pinned across POSIX jobs to avoid moving host tools
between stages; changing to these versions may require initial regeneration.
Checksum, native smoke, and object-retention evidence checks remain mandatory.
Compare completed-run elapsed times, planned Ninja work, and retention reports
before claiming a measured speedup.

Windows build entrypoints configure the bundled `third_party/node/win/node.exe`
before Chrome compilation, including checkpoint resumes. They append
`--disable-wasm-trap-handler` to `NODE_OPTIONS`, preserving existing options, so
Node uses explicit WebAssembly bounds checks for build tools such as Rollup.
This is a build-only compatibility workaround for the native `0xC0000005` exit
observed with Node 24.12.0 during DevTools highlighting bundling; the precise
cause of that crash is not yet established. It neither disables WebAssembly
nor replaces Node or changes Chromium build flags. A bundled-Node WASM probe
must succeed before compilation starts; invalid options or missing Node fail
the build rather than falling back to PATH or hiding compiler errors. Local
probe tests cover Node 24.12.0; the original workload still requires CI retesting.

The Windows reusable workflow retains its 12-stage snapshot/resume chain. Each
stage uploads multi-volume 7-Zip snapshots with modification times preserved so
Ninja can continue incrementally. Manual dispatch of
`.github/workflows/build-win-x64-github.yml` supports Windows-only retries and
cross-run resume independently of the other four workflows. Fresh Windows builds
now validate V8 Torque inside build-1 after preparation and GN generation, then
continue Chrome compilation on the same runner. Build stages retain a 300-minute
internal deadline and 40-minute handoff reserve within the 355-minute job.
The standalone `-ValidateOnly` option retains its 230-minute deadline and
15-minute diagnostic reserve, but is no longer invoked by the workflow. Windows
full-cache fetching is capped at 180 minutes and further limited by the actual
remaining budget, preserving the reserve and another 30 minutes for preparation.
The downloader's shared retry deadline remains 45 minutes. The previous 60-minute
total cap killed a fetch after its 15 GB download and outer ZIP extraction had
completed; it did not distinguish slow inner extraction from cleanup after an
unreported extraction error. Extraction now records phase timings, member count,
actual written bytes and free space at most once per 30 seconds during ordinary
progress, plus phase boundaries. Failure reasons are persisted before cleanup.
Windows forwards tracked stderr progress and reports the last fetch state on a
timeout. Log-copy tasks own their pipes and output files until EOF, including
when a child retains the parent's redirected handles. Process exit and log-drain
waits are bounded; an unconfirmed cleanup only prints fixed-size log excerpts and
keeps all Windows tree snapshot/upload steps disabled through `snapshot_safe`.
These diagnostics and the revised budget still require validation on the full
Windows runner workload. Miss reports
retain the last download attempt's partial and expected byte counts separately
from verified bytes.
Windows selects the host Git installation's `usr/bin/patch.exe` before PATH
alternatives. Every candidate must pass a small real patch test with the same
strict options used for application: zero fuzz, no version-control checkout,
CRLF preservation, dry-run, duplicate rejection, and reverse application.
An incompatible executable fails before a patch-in-progress marker is written;
`--version` alone is not accepted as a compatibility check. Cold and restored
preparation share this selection. An existing interrupted-patch marker still
requires a clean restored work directory.

V8 Torque validation uses only the remaining non-reserved time, remains serial
and verbose, and fails before Chrome compilation on error. Inline validation
does not report `finished=true`; only the completed bundle path may do so.
Artifact-resume stages continue through the normal Ninja dependency graph.

The POSIX reusable workflow follows the upstream ungoogled-chromium CI model:
portablelinux's `prep` + `build_part_01..10` chain and macOS'
`retrieve-resources` + `build_job_01..20` chain. In staged mode each POSIX target
can run `posix-1..posix-8`, with a fresh runner matching the platform table for
each stage. Resource recording precedes checkout and records CPU, RAM, disk, and
a job-start timestamp. The internal deadline is the earlier of 300 minutes from
stage-script start or 330 minutes from that timestamp, within the 355-minute job.
Compilation uses `timeout -k 7m -s SIGTERM` with a 45-minute handoff reserve
(15 minutes in single mode), leaving additional time for diagnostic/artifact
uploads. Setup and snapshot download time therefore reduce the remaining budget:

1. restore the previous stage's tree snapshot (none at stage 1);
2. run `build/posix/ci-stage.sh --platform --arch --stage-index ...`;
3. prepare the pinned ungoogled source (or resume it) under the deadline;
4. continue Ninja through `build/build.sh` / `build/macos/build.sh`;
5. on deadline exit 124, pack `${workdir}` with
   `build/posix/ci-parts.sh` into multi-volume `tree.tar.zst.*` files via
   `tar --format=pax | zstd`, preserving nanosecond mtimes, modes, and symlinks
   so incremental Ninja state survives; the packer
   excludes all snapshot staging directories and the separately cached
   `download_cache`, preventing the archive from reading its own output;
6. validate regular, nonempty, unique, contiguous numbered volumes and stream
   the complete zstd archive through `tar -t`, then upload up to
   four volume artifacts; the next stage downloads them with
   `actions/download-artifact@v4` (`merge-multiple: true`, numeric volume order)
   and resumes.

Both same-run and selected POSIX checkpoint restores use
`build/posix/restore-snapshot.sh`. Volume order depends on `.001`, `.002`, etc.,
never on upload slot directory names: round-robin slots contain `.001/.005`,
`.002/.006`, etc. Restores stage extraction before publishing, preserving the
separately restored root `download_cache`. Publication uses checked directory
renames with rollback on failure; replacing an existing tree is not a single
atomic swap. If rollback itself fails, the staging path is reported and retained
for recovery rather than deleting the original data.

Compile failures fail the job immediately. Insufficient preparation/compile
budget and timeout exit 124 save an unfinished checkpoint. Earlier stages hand
off successfully; the final allowed stage fails **after** packing and setting the
upload marker. Snapshot verification/upload steps run after this failure unless
the job is cancelled, while final bundle upload still requires completion. Failed
packing emits no upload marker. A saved terminal checkpoint can be selected in a
new Mac or Linux run without throwing away that last stage's compilation progress.
The POSIX stage scripts stay compatible with the system `/bin/bash` 3.2 that
runs GitHub's macOS workflow steps (no nested quoted command substitution
inside `$(( ))`, no bare GNU `timeout`/`split` - both resolve through a
`gtimeout`/`gsplit` Homebrew fallback like upstream), and the handoff plus
restore paths are executed end to end under a locally built real bash 3.2 in
the regression suite. `tree.tar.zst*` archives over eight volumes (eight
~9 GB slices) abort the chain rather than upload a broken handoff,
mirroring the Windows multi-volume guard; between five and eight volumes
the chain continues with an explicit warning instead of aborting, because
re-packing hundreds of gigabytes buys nothing once the per-artifact upload
cap is the real constraint. Later stage jobs require `always()`, a successful
predecessor whose `finished` output is not `true`, and a `max-stages` value that
includes their index. An early finish skips later stages while hard failures
stop the platform; single mode never schedules another compile stage.

Each POSIX stage also caches only the pinned source and resource downloads at
`${{ runner.temp }}/chromix-build/download_cache`. The cache key includes the
runner OS, platform, architecture, Chromium version, revisions, and preparation
script. The mutable Chromium `src/` and `out/` trees are intentionally not
placed in `actions/cache`: their size, runner/toolchain coupling, and file
metadata make a blind restore unreliable. A POSIX retry instead restores the
explicit tar/zstd stage snapshots — never a cache guess.

### Pinned upstream build-directory restoration

Fresh unified builds require the five upstream Actions artifacts pinned in
`build/upstream-cache.json` when `use_upstream_cache` is enabled. The manifest
records the repository, source commit, run, artifact ID, size, and SHA256 digest
for each native target. This is explicit cross-repository artifact download,
not shared `actions/cache` access. A manual unified dispatch can set
`use_upstream_cache` to false for a full source build. An optional
`UPSTREAM_ACTIONS_TOKEN` secret can grant access to public upstream Actions
artifacts; otherwise the workflow tries its own `github.token`. A 403, 404,
expired artifact, checksum failure, insufficient disk or time budget, or rejected
source identity stops a required-restoration build before cold preparation or
Ninja. Existing and resumed trees must carry a valid restoration receipt in this
mode, including Windows validation; a cold snapshot cannot satisfy the request.

Restoration runs **before fresh source preparation**, only when `WORK/src` is
absent. All five targets restore the complete upstream source tree and its
`src/out/Default`, including objects, generated inputs, `build.ninja`,
`.ninja_log`, and `.ninja_deps`. The directory is moved on the same filesystem,
not duplicated or renamed to `out/Chromix`. Ordinary fresh builds still use
`out/Chromix`; when packaging a restored build manually, use `out/Default` in
the commands above. Own-stage snapshots take precedence and never refetch the
upstream artifact.

Download and extraction occur in a separate, owned directory. POSIX full-tree
fetches have a 60-minute total deadline, configurable with
`CHROMIX_CACHE_TIMEOUT_SECONDS`; the old 20-minute toolchain-oriented limit
expired on the complete macOS Intel archive. Network attempts remain separately
bounded, and a total deadline expiry stops required-restoration builds.
Signed blob redirects receive no GitHub authorization header; the outer ZIP is
hashed before its inner archive is extracted. Unsafe archive members are rejected. Internal
absolute symlinks are remapped only from the pinned platform's known source
root to an existing internal target. Known external host-tool and Xcode SDK
links are omitted and recorded for recreation on the current runner. Source
version, architecture, manifest identity, and required Ninja state are verified
before an atomic installation; an existing source tree is never overwritten.
Exact whole-second output timestamps are repaired from trusted Ninja records
only when the recorded inputs prove they are unambiguous. Restoration accepts
Ninja log v5, v6, and v7 and leaves the original log bytes and command hashes
unchanged. v6/v7 use command-start timestamps (or restat timestamps), rather
than v5's output timestamp. v7 uses rapidhash; the optional legacy single-object
importer still rejects v7 instead of checking it with the v5/v6 hash algorithm.
Restore diagnostics retain available bounded log/dependency headers and file
sizes before source validation; missing or unsafe metadata is recorded without
following links, even when a rejected owned cache is removed. Unknown formats
still stop restoration. Counting, timestamp planning, and installation write
phase reports before starting, with elapsed time and available counters.
Timestamp planning validates each distinct successful input once, checks its
mtime against each output's own cutoff, then revalidates input identity before
returning the plan. This avoids repeating filesystem walks for shared headers;
it neither caches results across restores nor permits concurrent source edits.
Transient input I/O failures abort planning without deleting the donor. Caught
installation interruptions roll back the move and timestamp changes; if rollback
fails, the source is retained and its location is reported for recovery.

Before any restored-output Ninja invocation, the build selects one native
executable matching the log generation: Ninja 1.11 for v5, 1.12 for v6,
and 1.13 for v7. Ninja 1.10 can read v5 but lacks the input-query tool required
by retention diagnostics. Its path, version, architecture, and rejected candidates are
recorded in `upstream-cache-ninja.json`. The same executable runs the plan and
compile; an incompatible reader is never allowed to discard the old log.
Linux Actions also install checksum-pinned Ninja 1.12.1 for x64/arm64 alongside
the distro tool, since the upstream Debian tool can differ from Ubuntu's.
Selection still follows the actual restored header, not the target platform.

Linux and macOS builders run `gn --version` before reusing the restored GN.
A missing or unrunnable GN is bootstrapped in a fresh temporary directory under
`src/out`, with an explicit `--build-path`. The bootstrap must pass its own
`--version` probe before atomically replacing the output GN. This avoids copying
an incompatible cached executable from the bootstrap's default
`out/Release/gn_build`, as observed on the Intel runner in run `34295258278`.
Only the temporary GN build is removed; if restoring the previous GN itself
fails, the recovery directory is retained and reported. Chromium objects and
Ninja state are preserved, subject to the normal toolchain and dependency
invalidation checks.

The restored source already contains the pinned ungoogled core patches,
platform overlay, pruning, and domain substitution. Preparation verifies those
pins and appends Chromix `patches/series` without reapplying upstream layers.
`tools/apply_restored_patches.py` translates patch context and additions using
the pinned domain-substitution rules and applies with `--fuzz=0`. Interrupted
or mismatched patch markers fail instead of accepting a partially patched tree.

Native tool headers and executable probes check the actual runner architecture.
Restored macOS tools run with inherited `DYLD_*` overrides removed. Only the
bindgen child receives a temporary search directory containing a single symlink
to native, source-confined `libclang.dylib`; LLVM's bundled C++ libraries are not
exposed to clang, GN, Node, or system frameworks. This applies before the first
inspection to avoid treating loader-environment failures as tool changes.
A relative library link supplies nightly rust-objcopy's existing loader path
without changing executable bytes. A native, timed `rust-objcopy --version`
probe with all `DYLD_*` variables removed must pass before preparation finishes.
A SHA256-pinned, idempotent repair applies the same libclang-only isolation inside
the Mac bindgen action, after Ninja's system-shell invocation. Unknown wrapper
contents stop preparation rather than receiving a partial edit.
Required tool replacement happens only in Actions. A changed compiler/runtime
invalidates compiled outputs; unknown external or missing Ninja dependencies
invalidate their dependent outputs. Omitted host links listed in the verified
restore receipt remain external dependencies even before they are recreated.
Only the output-root SDK directories are protected from deletion; nested WebRTC
or DevTools directories named `sdk` are ordinary build outputs. Host generator
contents (Node, Go, gperf, and clang-format where used) are tracked separately.
Initial restoration or a changed generator removes logged outputs lacking compiler
dependency records, including generated headers and archive/link products,
without clearing unrelated C/C++ objects merely because a host link was restored.
Readers of removed generated inputs are invalidated separately; this conservative
step can require substantial recompilation. Runner/SDK identity is rechecked
on resume.
The first preparation removes upstream final browser products to force a new
Chromix link. Chromix GN settings override restored arguments, GN regenerates
`out/Default` for the current environment, and Ninja compiles incrementally.
Matching Chromium versions alone does not prove an object is reusable: different
flags, host tools, SDKs, or dependency paths can require substantial recompilation.

Diagnostics include `upstream-cache-restore.json`,
`upstream-cache-preparation.json`, the source's `.chromix-upstream-restored.json`
receipt, the downloader's `result.json`, and `upstream-cache-plan.log` from
`ninja -n`. Preparation writes the uploadable report during inspection and before
finalization, so failed native probes retain their exact output and are marked
`ready_for_gn: false` rather than leaving a stale success report. Diagnostic
uploads include the explicitly listed hidden restoration and patch receipts.
Downloader phase logs distinguish metadata checks, download, outer
archive unpacking, full source/object extraction, and source verification.
Disk rejection preserves free/required bytes, the failure phase, and any written
extraction bytes/member before cleanup; a cleaned-up miss is not reported as a
zero-byte extraction attempt. The preparation report records invalidated outputs
and removed final products. It includes bounded input-path examples and counts
of output readers affected by omitted external links, other external inputs,
missing local inputs, and intentionally removed generated inputs; these categories
can overlap for one output. Timestamp-repair diagnostics separately retain up to
32 skipped output/input examples before preparation cleanup.
In native macOS ARM64 run `34291301280`, these diagnostics attributed 46,151
outputs to omitted SDK dependencies, including
`sdk/xcode_links/MacOSX26.0.sdk/SDKSettings.json`; all 45,769 eligible object
candidates were missing by the first build baseline. That run reached actual
compilation, but established no object retention. A matching SDK directory name
alone is not enough to exempt these dependencies from invalidation.
A successful restoration is not proof of object hits,
and a dry-run
count is not a measured speedup. Native CI must establish actual reuse and
elapsed time; local regression tests use small fixtures and sparse patch checks,
without downloading full build artifacts or compiling Chromium. Artifact expiry
requires reviewing and updating the pinned manifest rather than silently
selecting the newest upload.

`upstream-reuse/baseline.json` preserves a bounded sample before the first actual
Chromix Ninja build, after GN and the plan. It samples at most 128 `.o`/`.obj`
inputs of the requested targets, hashing at most 64 MiB total and 8 MiB per file.
Nonlocal or unsupported object-looking closure inputs are excluded from sampling
and recorded in bounded diagnostics; their bytes remain in the full input digest.
They are never normalized into local files or counted as retained objects.
The baseline records full Ninja log entries, hashes, sizes, and nanosecond mtimes;
resumed stages retain it rather than sampling newly compiled objects. Each actual
invocation writes `upstream-reuse/result.json` with its exit code and a comparison
to that original baseline. Both reports must survive handoffs. Each observation
verifies the previous complete log prefix, and any observed rebuild or lost log
continuity permanently disqualifies the affected samples. Any new selected-output
record disqualifies that sample, even if every recorded field repeats exactly.
An unsupported object-output spelling appended after the baseline disqualifies every sample,
because it may name a sampled file through an alias. Such paths are never
resolved to make them eligible. Only unchanged samples in the target inputs after a
successful invocation count as observed retention. Failed or timed-out builds,
changed graphs, truncated logs, and zero retained samples do not establish reuse.
These small reports are uploaded on every build stage. This measures retention
since the first Chromix build in a verified upstream source tree; it does not
independently reconstruct the donor's per-object history or imply a whole-tree
cache-hit percentage. The requested target's dependency closure can include
host-tool objects, so a positive generic retained count alone does not establish
ARM64 object reuse on an x64 host. Architecture-qualified evidence must match the
retained object's contents, not its directory name or the target receipt.
`architecture_evidence` in the result reports Linux ELF64 little-endian relocatable
objects by architecture, with `target_retained_count` and `target_retention_proven`
separate from generic retention. Bitcode and unsupported formats remain `unknown`;
a zero target count means no proven target objects in this sample, not no reuse
elsewhere. Classification uses the same fully hashed bytes and does not replace
or resample an existing baseline.

Host toolchains follow the host architecture, not the target: Node resolves
through `third_party/node/linux/node-linux-$HOST_ARCH/bin/node` with an extra
x64 link for hardcoded generator paths, Go is linked as
`third_party/dawn/tools/golang/linux-amd64/bin/go` on x64 hosts and
`linux-arm64` on arm64 hosts (Dawn's DEPS pins exactly these cipd directories),
matching upstream portablelinux's `setup_toolchain`. Only GN args, sysroots,
and output binaries select the target architecture. POSIX stages install Go
`1.27.1` through `actions/setup-go` and verify its version on PATH, meeting Dawn's
`go.mod` requirement of go 1.25.0 toolchain support. The action selects the host
architecture. This avoids relying on older distro Go or changing the generator
version between stages; no compiled-object cache is provided by this action.

The final POSIX stage verifies `SHA256SUMS` and extracts the ZIP into a fresh
directory using the SDK's checked extractor, preserving executable permissions
and rejecting unsafe members and links. Native builds run the extracted launcher with `--version` and a bounded
headless `--dump-dom` check against a local data URL. Linux ARM64 cross-builds
perform the ELF architecture checks there and require the separate native ARM64
job described above for both runtime checks. These checks use the pinned
browser version and rendered marker. The bundle is uploaded by the compile
stage, but the overall workflow still waits for required native verification.
On Linux CI hosts with AppArmor's unprivileged-user-namespace restriction,
`build/linux/prepare-ci-sandbox.sh` loads a path-specific profile for the extracted
browser before the smoke test. It leaves global sysctls and executable privilege
bits unchanged; an unavailable sandbox remains a failure. The workflow
does not launch from the build output, contact a test website, disable the
sandbox, or notarize macOS bundles. Sandbox/user-namespace policy, missing
shared libraries, or macOS launch restrictions can fail the check; such
failures are not silently skipped. This small check does not validate GPU
operation, GUI behavior, Playwright integration, Gatekeeper approval, or
every supported OS version.

The final Windows stage also verifies the ZIP checksum before fresh extraction.
It checks the numeric product-version resources of both `chrome.exe` and
`chrome.dll`, because Chromium's command-line `--version` handler is POSIX-only.
If a versioned DLL directory would take precedence, its DLL must match the newly
linked portable DLL byte-for-byte. A bounded `cmd.exe` invocation runs the
extracted `chromix.cmd` with the same local headless DOM check; version metadata
alone is not a launch test.

Preparation, build, packaging, checksum, and smoke output are captured in
per-stage log artifacts alongside generated `args.gn` when present.
Diagnostics upload uses `always()` so ordinary failed steps still upload logs.
Tree uploads run only after a successful deadline handoff, never after a hard
compile failure or final completion. The workflow does not pack an already
packed handoff a second time. This avoids delaying failure reporting and
unnecessary multi-gigabyte uploads; the failed macOS Intel job in run
34204781261 spent ten minutes in an upload that ended with `Upload progress
stalled`. Completed handoff uploads still depend on network availability and
artifact limits; a failed upload stops the chain. Hard job termination, runner
loss, or a full disk can prevent diagnostic uploads too.

**CI cost and capacity:** filtered pushes to `main` start affected platforms;
a manual dispatch starts only its selected platform. In staged mode, each POSIX
job has a 355-minute limit and retry capacity comes from resumable snapshots
instead of repeated full rebuilds. Single mode keeps that job limit but has no
unfinished-tree recovery. A full staged run can consume
far more runner-minutes than the earlier one-shot layout before billing
multipliers/quota rules; macOS is typically more expensive where usage is
billed. `cancel-in-progress: false` does not cancel an active run when newer
work arrives. Hosted disk/RAM may be insufficient even after Linux cleanup,
especially for the arm64 LLVM/Rust bootstrap, link steps, and duplicate
packaging/extraction trees. Do not treat cleanup or a 100 GB estimate as proof
of capacity.

Each successful platform run uploads its browser ZIP and `SHA256SUMS` as an
Actions artifact retained for 14 days. `release-browser.yml` reacts only to
successful completions of the five named platform entrypoints on this
repository's `main` branch, from push or manual-dispatch events. A read-only
readiness job validates the original successful event's workflow path, repository,
source SHA, run, and attempt, then derives its Chromium version. Publication jobs
are serialized per validated version, so different versions cannot replace one
another in GitHub's pending queue. A later rerun does not discard the original
event's version-wide reconciliation. Each discovered platform candidate must
still be the latest successful run/attempt at its own SHA; a newer failed or
running attempt blocks older success at that SHA, without hiding eligible success
at another SHA. Other platforms' states and source SHAs do not gate publication.
Candidate readiness is checked again after the queue and after all CI-side
downloads. Release tooling is checked out from trusted `main` once, then pinned
to that same tooling commit for publication; build-source SHAs remain separate
provenance inputs.

GitHub keeps only one pending job per concurrency group. Every surviving release
job therefore reconciles all missing platform slots for the requested Chromium
version, including eligible independent runs from different source SHAs, instead
of relying only on its triggering event. Discovery inspects at most ten 100-run
pages per platform and 200 distinct source versions, and fails explicitly on an
incomplete scan before publication. Existing checksummed slots are preserved.
A platform-local artifact download or validation failure is reported while other
eligible platforms are still processed; shared release-integrity errors stop
publication. A release-only catch-up can be dispatched without starting or
retrying any build:

```bash
gh workflow run release-browser.yml --ref main -f version=152.0.7977.82
```

Publication validates the incoming checksum, required ZIP layout and licenses,
and every member's CRC before appending that platform's ZIP to `v<CHROMIUM_VERSION>`.
The incoming source and the existing tag's commit must both declare the tag's
Chromium version. Different source SHAs are explicitly allowed for that version;
notes record each platform's exact workflow, run, attempt, and source commit,
without claiming a common source revision. The tag remains pinned to its initial
source commit and the title remains `Chromix <version>`. Existing browser ZIPs,
sidecar licenses, other assets, and recorded hashes are retained; different bytes
under an existing browser asset name are rejected. Existing manifest entries
are verified and preserved when new checksums are appended. Before replacing
`SHA256SUMS`, publication stores and verifies immutable
`SHA256SUMS.backup.<sha256>` assets for both manifest versions. A consistent
append-only backup history can restore a missing primary or complete an
interrupted append after rollback, independently of the next incoming platform.
All recovered entries must match the existing release files before restoration.
An uploaded browser without a recorded checksum is recoverable only by matching
the exact incoming successful run's freshly downloaded and validated artifact;
unrelated unrecorded browser assets stop publication. Public build provenance
is written before saving the new manifest backup. New releases stay
drafts until the incoming assets and manifest are uploaded successfully.

Old aggregate runs are not automatically adopted by this change. In particular,
POSIX aggregate run `34308090891` retains its old workflow name; a failed aggregate
completion is never consumed even if an individual platform uploaded an artifact.
The user-selected Windows artifact `10066146011` from successful run `34080799322`
(source `23fd0a7a0c63cd452cfaec6b2aba8469ef5d4123`) is a separate manual verification
and publication to `v152.0.7977.82`, titled `Chromix 152.0.7977.82`, with its ZIP
unchanged, sidecar licenses, and `SHA256SUMS`. Successful platform artifacts from
old aggregate runs also require manual verification and append to that same tag.
Future independent platform successes use the automatic path above. No Windows
retry is authorized as part of this release change; SDK package versions and
release-channel pins are unchanged.

### Verify and run a POSIX candidate

Download the matching Actions artifact and unwrap GitHub's artifact envelope
first, leaving the inner browser ZIP and `SHA256SUMS` in the same directory.
Do not checksum the outer Actions download against the inner manifest. Each
matrix artifact has its own manifest; keep them separate or merge deliberately.

Choose the checksum tool explicitly: GNU `sha256sum -c SHA256SUMS` on Linux,
Perl `shasum -a 256 -c SHA256SUMS` on macOS. A checksum failure must stop the
process; do not retry a different tool after a mismatch. Then use native `unzip`
(or another extractor preserving executable bits and macOS framework symlinks)
to extract the matching ZIP into a fresh directory. The launcher is
`chromix/chromix` under that directory on both POSIX platforms. Run it with
`--version`, then `--headless --disable-gpu --no-first-run --dump-dom
'data:text/html,<p>chromix-smoke-ok</p>'` and a new `--user-data-dir`. CI bounds
these invocations to 30 and 60 seconds respectively. Run as a normal user with
a working sandbox; do not add `--no-sandbox` to turn a failure into a pass.
See [README.md](README.md#use-a-local-browser-binary) for the SDK's actual local
binary API instead of waiting for a POSIX Release download.

## Native Windows build

Prerequisites:

- Visual Studio 2022 with the Desktop development with C++ workload
- Windows 11 SDK 10.0.26100 and its Debugging Tools feature
- Python 3, Git, PowerShell 7, and 7-Zip
- about 120 GB free disk space

Run from the repository root in a Developer PowerShell:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Jobs 8
```

The script performs these layers in order:

1. checks out the pinned ungoogled-chromium repositories;
2. downloads and unpacks the pinned Chromium source archive;
3. downloads Windows toolchain dependencies and assembles the downloaded x64 Rust
   components into Chromium's `third_party/rust-toolchain` layout;
4. applies ungoogled-chromium core patches;
5. applies the ungoogled-chromium-windows overlay;
6. prunes binaries using the ungoogled pruning list;
7. applies every Chromix patch in `patches/series`;
8. merges ungoogled common GN flags, Windows flags, and `build/args.windows.gn`;
9. bootstraps GN and bindgen, then builds `chrome` with Ninja.

The output browser is:

```text
D:\chromix-build\src\out\Chromix\chrome.exe
```

Resume an interrupted compile with the same work directory:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -Jobs 8
```

For GitHub Actions, stage 1 defaults to the pinned Windows `build-artifact`
through `use_upstream_cache`. An optional `upstream_run_id` must equal the run in
`build/upstream-cache.json`; it cannot select an arbitrary upload. The
`UPSTREAM_ACTIONS_TOKEN` secret grants Actions read access. Windows restores the
complete pinned source and `out/Default`, appends Chromix patches, and regenerates
GN arguments for the current runner. Compiler/runtime changes invalidate compiled
outputs; missing or external SDK dependencies invalidate their readers. Compatible
objects and Ninja state remain available for incremental compilation, and upstream
final browser binaries are removed to force a Chromix link. This full-tree path
is separate from the legacy optional bindgen/object importer.

`-Resume` still validates the prepared source marker against the current
ungoogled pins and patch-content hash. A stale or mixed source tree is rejected.

## Domain substitution

Linux and macOS apply ungoogled domain substitution by default after all source
and platform toolchain resources have been downloaded. Set
`CHROMIX_APPLY_DOMAIN_SUBSTITUTION=0` only for build debugging. Windows Actions
apply the Windows overlay's domain list after toolchain bootstrap/bindgen and
before GN generation, with explicit interruption and completion markers. The
standalone Windows build keeps substitution explicit; use:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -ApplyDomainSubstitution
```

The default build already uses ungoogled source patches and GN flags to disable
Google integrations and reporting; the optional substitution additionally
rewrites domains listed by the Windows overlay.

## Windows x64 package

```powershell
pwsh build/windows/package-win.ps1 `
  -Out D:\chromix-build\src\out\Chromix `
  -Dest D:\chromix-build\dist
```

This creates `chromix-win-x64.zip` and `SHA256SUMS`. The archive contains
`chromix/chromix.cmd` for manual launch; the Python and Node SDKs resolve the
underlying `chromix/chrome.exe` executable.

## Patch maintenance

Chromix patches are Chromium 152 ports of the Clearcote-derived engine changes.
Clearcote is the behavioral reference, but its Chromium 149 patch files are
rebased and split into one target file per patch before entering `patches/series`;
applying the original directory on top would duplicate writers and target the
wrong source revision.

The Clearcote Runtime suppression, remote Canvas/WebGL Bridge, and fake WebRTC
srflx ports are compiled in but default-off. See `patches/README.md` for their
explicit `--uxr-*` switches and operational risks.

Check repository patch invariants with:

```powershell
python tools/check_patches.py
```

Patch application requires a clean prepared source tree and is checked by
`build/windows/prepare-ungoogled.ps1` or `build/prepare-ungoogled.sh`. Passing
patch application does not establish that GN generation, compilation, or runtime
checks will succeed.
