#!/usr/bin/env python3
"""Generate .github/workflows/build-posix-github.yml deterministically.

The generator exists because posix-2..posix-8 must stay byte-identical apart
from stage numbers; editing eight job blocks by hand drifts. Tests in
tools/tests/test_cross_platform_build.py assert the generated file matches a
fresh run of this script.
"""
from pathlib import Path

OUT = Path(".github/workflows/build-posix-github.yml")
STAGES = 8

HEADER = """# GitHub-hosted POSIX (Linux x64/arm64, macOS x64/arm64) build, modeled on
# ungoogled-chromium-portablelinux's prep/build_part_01..10 chain and
# ungoogled-chromium-macos' retrieve-resources/build_job_01..20 chain: a full
# Chromium build exceeds the 6h single-job limit, so posix-1..posix-8 run
# under a self-imposed ~5h budget via `timeout -k`, then snapshot the work
# tree with tar|zstd (mtimes preserved for ninja) uploaded as artifacts; the
# next stage restores and resumes ninja incrementally until it returns 0.
name: build-posix-github

on:
  workflow_call:
    inputs:
      platform:
        required: true
        type: string
      arch:
        required: true
        type: string
      runner:
        required: true
        type: string
      artifact:
        required: true
        type: string
      max-stages:
        required: false
        type: number
        default: 8
      build_profile:
        required: false
        type: string
        default: fast
      compile_jobs:
        required: false
        type: string
        default: auto
      resume_run_id:
        required: false
        type: string
        default: ''
      resume_tree_stage:
        required: false
        type: string
        default: '7'
      resume_artifact_ids:
        required: false
        type: string
        default: ''
      resume_attempt:
        required: false
        type: string
        default: '1'
      use_upstream_cache:
        required: false
        type: boolean
        default: true
    secrets:
      UPSTREAM_ACTIONS_TOKEN:
        required: false

permissions:
  contents: read
  actions: read

concurrency:
  # Isolate caller workflows without cancelling the legacy aggregate run.
  group: build-posix-${{ github.workflow }}-${{ inputs.platform }}-${{ inputs.arch }}-${{ github.ref }}
  cancel-in-progress: false

env:
  DEPOT_TOOLS_METRICS: '0'
  DEPOT_TOOLS_COLLECT_METRICS: '0'
  CHROMIUM_VERSION: '152.0.7977.82'
  CHROMIX_JOBS: ${{ inputs.compile_jobs }}

jobs:
"""

LINUX_CLEAN = """      - name: Free Linux disk space
        if: runner.os == 'Linux'
        run: |
          sudo rm -rf /usr/local/lib/android /usr/local/.ghcup /usr/lib/jvm \\
            /usr/local/share/boost /usr/share/swift \\
            /usr/lib/dotnet /usr/lib/google-cloud-sdk
          sudo docker system prune -af || true
          df -h

      # Match Chromium's install-build-deps; Go is pinned by setup-go below.
      - name: Install Linux build dependencies
        if: runner.os == 'Linux'
        run: |
          set -euo pipefail
          sudo apt-get update
          sudo apt-get install -y \\
            apparmor-utils bison clang clang-format cmake curl flex g++ git gperf \\
            libasound2-dev libatk1.0-dev libcups2-dev libdrm-dev libegl1-mesa-dev \\
            libevent-dev libflac-dev libgbm-dev libglib2.0-dev libgtk-3-dev \\
            libjpeg-dev libnss3-dev libopus-dev libpam0g-dev libpci-dev \\
            libpipewire-0.3-dev libpulse-dev libspeechd-dev libudev-dev \\
            libva-dev libvpx-dev libwebp-dev libx11-xcb-dev libxcb-dri3-dev \\
            libxshmfence-dev libxslt1-dev libxss-dev libxtst-dev mesa-common-dev \\
            ninja-build pkg-config python3-jinja2 python3-pyparsing \\
            python3-setuptools python3-six rsync uuid-dev xz-utils yasm zip unzip \\
            zstd patch file

      - name: Install restored-build Ninja v6
        if: runner.os == 'Linux' && inputs.use_upstream_cache
        run: |
          set -euo pipefail
          case "$(uname -m)" in
            x86_64)
              NINJA_ARCHIVE=ninja-linux.zip
              NINJA_SHA256=6f98805688d19672bd699fbbfa2c2cf0fc054ac3df1f0e6a47664d963d530255 ;;
            aarch64)
              NINJA_ARCHIVE=ninja-linux-aarch64.zip
              NINJA_SHA256=5c25c6570b0155e95fce5918cb95f1ad9870df5768653afe128db822301a05a1 ;;
            *) exit 1 ;;
          esac
          NINJA_DIR="$(mktemp -d "${RUNNER_TEMP}/chromix-ninja-v6.XXXXXX")"
          curl --fail --location --retry 3 --max-time 120 --max-filesize 2097152 \\
            "https://github.com/ninja-build/ninja/releases/download/v1.12.1/${NINJA_ARCHIVE}" \\
            -o "${NINJA_DIR}/ninja.zip"
          printf '%s  %s\\n' "$NINJA_SHA256" "${NINJA_DIR}/ninja.zip" | sha256sum --check --strict
          unzip -q "${NINJA_DIR}/ninja.zip" ninja -d "$NINJA_DIR"
          rm "${NINJA_DIR}/ninja.zip"
          chmod +x "${NINJA_DIR}/ninja"
          test "$("${NINJA_DIR}/ninja" --version)" = 1.12.1
          printf '%s\\n' "$NINJA_DIR" >> "$GITHUB_PATH"
"""

MAC_STEPS = """      - name: Select compatible Xcode
        if: runner.os == 'macOS'
        run: bash build/macos/select-xcode.sh

      - name: Free macOS disk space
        if: runner.os == 'macOS'
        run: |
          set -euo pipefail
          python3 build/macos/free-disk-space.py 2>&1 | tee "${RUNNER_TEMP}/chromix-logs/disk-cleanup.log"

      - name: Inspect macOS toolchain
        if: runner.os == 'macOS'
        run: |
          {
            sw_vers
            xcode-select -p
            xcodebuild -version
            xcrun --sdk macosx --show-sdk-version
            xcrun --sdk macosx --show-sdk-path
          } 2>&1 | tee "${RUNNER_TEMP}/chromix-logs/xcode.log"

      - name: Disable Spotlight indexing
        if: runner.os == 'macOS'
        run: sudo mdutil -a -i off

      # Chromium supplies clang; Homebrew supplies the remaining build tools.
      - name: Install macOS build tools
        if: runner.os == 'macOS'
        run: brew install ninja coreutils gpatch zstd
"""

NODE_PY = """      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '24.20.0'

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: '1.27.1'
          cache: false

      - name: Verify Go version
        run: |
          go version
          test "$(go env GOVERSION)" = go1.27.1

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.13'
"""

SDK_PREFLIGHT = """      - name: Verify complete Mac SDK contents
        if: runner.os == 'macOS' && inputs.use_upstream_cache
        run: python3 tools/inspect_macos_sdk.py --report "${RUNNER_TEMP}/chromix-logs/sdk-content.json"
"""

CACHE_RESTORE = """      # The pinned download cache is deliberately outside the tree snapshot;
      # re-warm it first so a resumed preparation does not redownload archives.
      - name: Restore pinned source downloads
        uses: actions/cache@v4
        with:
          path: ${{ runner.temp }}/chromix-build/download_cache
          key: ${{ runner.os }}-${{ inputs.platform }}-${{ inputs.arch }}-downloads-v2-${{ env.CHROMIUM_VERSION }}-${{ hashFiles('build/ungoogled-revisions.psd1', 'build/prepare-ungoogled.sh') }}
          restore-keys: |
            ${{ runner.os }}-${{ inputs.platform }}-${{ inputs.arch }}-downloads-v2-
"""


def run_step(stage: int) -> str:
    # GHA expressions use ${{ ... }}, so f-strings cannot carry them (their
    # braces would collapse to single braces). Use token replacement instead.
    # For stage 1 no restore argument exists; the continuation chain must stay
    # valid shell, so the token sits inline rather than as a standalone line.
    if stage > 1:
        restore_args = (
            '--from-snapshot "${RUNNER_TEMP}/chromix-restore" '
            "\\\n            "
        )
    else:
        restore_args = ""
    return (
        """      - name: Run stage __STAGE__
        id: stage
        env:
          CHROMIX_BUILD_PROFILE: ${{ inputs.build_profile }}
          CHROMIX_RESERVE_MINUTES: ${{ inputs['max-stages'] == 1 && '15' || '45' }}
          CHROMIX_USE_UPSTREAM_CACHE: ${{ inputs.use_upstream_cache && '1' || '0' }}
          GH_TOKEN: ${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}
        run: |
          set -euo pipefail
          NOW_EPOCH="$(date +%s)"
          DEADLINE_EPOCH=$(( NOW_EPOCH + 300 * 60 ))
          JOB_DEADLINE_EPOCH=$(( CHROMIX_JOB_START_EPOCH + 330 * 60 ))
          if [ "$DEADLINE_EPOCH" -gt "$JOB_DEADLINE_EPOCH" ]; then
            DEADLINE_EPOCH="$JOB_DEADLINE_EPOCH"
          fi
          build/posix/ci-stage.sh \\
            --platform '${{ inputs.platform }}' --arch '${{ inputs.arch }}' \\
            --workdir "${RUNNER_TEMP}/chromix-build" \\
            --stage-index __STAGE__ --max-stages '${{ inputs['max-stages'] }}' __RESTORE_ARGS__--deadline-epoch "$DEADLINE_EPOCH" \\
            2>&1 | tee "${RUNNER_TEMP}/chromix-logs/stage-__STAGE__.log"
"""
    ).replace("__STAGE__", str(stage)).replace("__RESTORE_ARGS__", restore_args)

DOWNLOAD_STEP = """      - name: Download tree from previous stage
        if: success()
        uses: actions/download-artifact@v4
        with:
          pattern: ${{ inputs.artifact }}-tree-s%(prev)d-attempt-${{ needs.posix-%(prev)d.outputs.snapshot_attempt }}-part*
          merge-multiple: true
          path: ${{ runner.temp }}/chromix-restore
"""

RESUME_STEPS = """      - name: Validate selected POSIX checkpoint
        id: resume
        if: inputs.resume_run_id != ''
        env:
          GH_TOKEN: ${{ github.token }}
          SNAPSHOT_RUN_ID: ${{ inputs.resume_run_id }}
          SNAPSHOT_STAGE: ${{ inputs.resume_tree_stage }}
          SNAPSHOT_ATTEMPT: ${{ inputs.resume_attempt }}
          SNAPSHOT_ARTIFACT_IDS: ${{ inputs.resume_artifact_ids }}
          BUILD_PLATFORM: ${{ inputs.platform }}
          CACHE_REQUIRED: ${{ inputs.use_upstream_cache }}
          RECOVERY_BRANCH: ${{ github.ref_name }}
        run: |
          set -euo pipefail
          test "$CACHE_REQUIRED" = true
          python3 tools/validate_posix_snapshot.py --platform "$BUILD_PLATFORM" --arch '${{ inputs.arch }}' \\
            --recovery-branch "$RECOVERY_BRANCH" \\
            --report "${RUNNER_TEMP}/chromix-logs/snapshot-origin.json"
      - name: Check out checkpoint patch definitions
        if: inputs.resume_run_id != ''
        uses: actions/checkout@v4
        with:
          ref: ${{ steps.resume.outputs.head_sha }}
          path: .chromix-previous-repo
          persist-credentials: false
      - name: Download selected POSIX checkpoint
        if: inputs.resume_run_id != ''
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          python3 tools/download_posix_snapshot.py \\
            --manifest "${RUNNER_TEMP}/chromix-logs/snapshot-origin.json" \\
            --destination "${RUNNER_TEMP}/chromix-restore" \\
            --report "${RUNNER_TEMP}/chromix-logs/snapshot-download.json"
      - name: Restore and migrate selected POSIX checkpoint
        if: inputs.resume_run_id != ''
        run: |
          set -euo pipefail
          RESTORE="${RUNNER_TEMP}/chromix-restore"
          WORK="${RUNNER_TEMP}/chromix-build"
          test ! -e "$WORK/src"
          bash build/posix/restore-snapshot.sh "$RESTORE" "$WORK"
          python3 tools/migrate_restored_snapshot.py --workdir "$WORK" \\
            --previous-repo "$GITHUB_WORKSPACE/.chromix-previous-repo" \\
            --repo "$GITHUB_WORKSPACE" --platform '${{ inputs.platform }}' --arch '${{ inputs.arch }}' \\
            2>&1 | tee "${RUNNER_TEMP}/chromix-logs/snapshot-migration.log"
"""

RUNTIME_FAILURE = """      - name: Upload failed macOS runtime bundle
        if: ${{ !cancelled() && inputs.platform == 'macos' && steps.stage.outcome == 'failure' && steps.stage.outputs.package_ready == 'true' && steps.stage.outputs.runtime_failed == 'true' }}
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-failed-runtime-s%(stage)d-attempt-${{ github.run_attempt }}
          path: |
            ${{ runner.temp }}/chromix-build/dist/${{ inputs.artifact }}.zip
            ${{ runner.temp }}/chromix-build/dist/SHA256SUMS
            ${{ runner.temp }}/chromix-build/runtime-smoke-stage-%(stage)d/
          if-no-files-found: error
          retention-days: 14
          compression-level: 0

      - name: Preserve failed macOS runtime checkpoint
        id: runtime_checkpoint
        if: ${{ !cancelled() && inputs.platform == 'macos' && steps.stage.outcome == 'failure' && steps.stage.outputs.package_ready == 'true' && steps.stage.outputs.runtime_failed == 'true' }}
        run: |
          set -euo pipefail
          WORK="${RUNNER_TEMP}/chromix-build"
          bash build/posix/ci-parts.sh "$WORK" "$WORK/.snapshot-stage-%(stage)d"
          echo "upload_snapshot=true" >> "$GITHUB_OUTPUT"

"""

SNAPSHOT_ENSURE = """      - name: Verify handoff snapshot
        id: checkpoint
        if: ${{ !cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || steps.runtime_checkpoint.outputs.upload_snapshot == 'true') }}
        run: |
          set -euo pipefail
          SNAP="${RUNNER_TEMP}/chromix-build/.snapshot-stage-%(stage)d"
          test -d "$SNAP/p1"
          python3 tools/snapshot_volumes.py "$SNAP" |
            xargs -0 cat | zstd -d -T0 | tar -tf - >/dev/null
"""

UPLOAD_PARTS = """      - name: Upload tree part 1
        if: ${{ !cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || steps.runtime_checkpoint.outputs.upload_snapshot == 'true') && steps.checkpoint.outcome == 'success' }}
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part1
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p1/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 2
        if: ${{ !cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || steps.runtime_checkpoint.outputs.upload_snapshot == 'true') && steps.checkpoint.outcome == 'success' }}
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part2
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p2/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 3
        if: ${{ !cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || steps.runtime_checkpoint.outputs.upload_snapshot == 'true') && steps.checkpoint.outcome == 'success' }}
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part3
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p3/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 4
        if: ${{ !cancelled() && (steps.stage.outputs.upload_snapshot == 'true' || steps.runtime_checkpoint.outputs.upload_snapshot == 'true') && steps.checkpoint.outcome == 'success' }}
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part4
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p4/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
"""

FINAL_UPLOADS = """      - name: Upload final bundle
        if: steps.stage.outputs.finished == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}
          path: |
            ${{ runner.temp }}/chromix-build/dist/${{ inputs.artifact }}.zip
            ${{ runner.temp }}/chromix-build/dist/SHA256SUMS
          if-no-files-found: error
          retention-days: 14
          compression-level: 0

      - name: Upload build diagnostics
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-logs-s%(stage)d-attempt-${{ github.run_attempt }}
          include-hidden-files: true
          path: |
            ${{ runner.temp }}/chromix-logs/
            ${{ runner.temp }}/chromix-build/runtime-smoke-stage-%(stage)d/
            ${{ runner.temp }}/chromix-build/src/out/Chromix/args.gn
            ${{ runner.temp }}/chromix-build/src/out/Default/args.gn
            ${{ runner.temp }}/chromix-build/src/.chromix-upstream-restored.json
            ${{ runner.temp }}/chromix-build/src/.chromix-restored-patches.json
            ${{ runner.temp }}/chromix-build/upstream-cache-restore.json
            ${{ runner.temp }}/chromix-build/upstream-cache-preparation.json
            ${{ runner.temp }}/chromix-build/upstream-cache-ninja.json
            ${{ runner.temp }}/chromix-build/upstream-cache-import.json
            ${{ runner.temp }}/chromix-build/upstream-cache-plan.log
            ${{ runner.temp }}/chromix-build/upstream-reuse/
            ${{ runner.temp }}/chromix-build/upstream-object-cache.json
            ${{ runner.temp }}/chromix-upstream/result.json
          if-no-files-found: warn
          retention-days: 14
"""


def job(stage: int) -> str:
    needs = f"posix-{stage - 1}" if stage > 1 else None
    # Job titles also carry GHA expressions; build them without f-strings.
    if stage == 1:
        title = "${{ inputs.platform }}-${{ inputs.arch }} stage 1 (prepare + first compile)"
    else:
        title = (
            "${{ inputs.platform }}-${{ inputs.arch }} stage "
            + str(stage)
            + " (resume compile)"
        )
    parts = [f"  posix-{stage}:\n", f"    name: {title}\n"]
    if needs:
        parts.append(
            "    needs: %s\n    if: >-\n"
            "      always() && inputs['max-stages'] >= %d &&\n"
            "      needs.%s.result == 'success' &&\n"
            "      needs.%s.outputs.finished != 'true'\n" % (needs, stage, needs, needs)
        )
    parts.append("    runs-on: ${{ inputs.runner }}\n")
    parts.append("    timeout-minutes: 355\n")
    parts.append("    outputs:\n")
    parts.append("      finished: ${{ steps.stage.outputs.finished }}\n")
    parts.append("      snapshot_attempt: ${{ steps.snapshot_origin.outputs.attempt }}\n")
    parts.append("    steps:\n")
    parts.append("      - name: Record runner resources\n")
    parts.append("""        run: |
          set -euo pipefail
          echo "CHROMIX_JOB_START_EPOCH=$(date +%s)" >> "$GITHUB_ENV"
          mkdir -p "${RUNNER_TEMP}/chromix-logs"
          {
            uname -a
            case "$(uname -s)" in
              Linux) getconf _NPROCESSORS_ONLN; free -h ;;
              Darwin) sysctl hw.ncpu hw.memsize ;;
            esac
            df -h
          } 2>&1 | tee "${RUNNER_TEMP}/chromix-logs/runner.log"

""")
    parts.append("      - uses: actions/checkout@v4\n\n")
    parts.append('      - name: Record snapshot producer attempt\n'
                 '        id: snapshot_origin\n'
                 '        run: echo "attempt=$GITHUB_RUN_ATTEMPT" >> "$GITHUB_OUTPUT"\n')
    parts.append(LINUX_CLEAN)
    parts.append(MAC_STEPS)
    parts.append(NODE_PY)
    parts.append(SDK_PREFLIGHT)
    if stage == 1:
        parts.append(RESUME_STEPS)
    if stage > 1:
        parts.append(DOWNLOAD_STEP % {"prev": stage - 1})
        parts.append("\n")
    parts.append(CACHE_RESTORE)
    parts.append("      - name: Select compile parallelism\n"
                 "        run: python3 tools/build_resources.py --github-env\n\n")
    parts.append(run_step(stage))
    parts.append(RUNTIME_FAILURE % {"stage": stage})
    parts.append(SNAPSHOT_ENSURE % {"stage": stage})
    parts.append("\n")
    parts.append(UPLOAD_PARTS % {"stage": stage})
    parts.append(FINAL_UPLOADS % {"stage": stage})
    return "".join(parts)


NATIVE_LINUX_ARM64 = """  verify-linux-arm64:
    name: Linux ARM64 native bundle verification
    needs: [posix-1, posix-2, posix-3, posix-4, posix-5, posix-6, posix-7, posix-8]
    if: >-
      always() && inputs.platform == 'linux' && inputs.arch == 'arm64' &&
      !contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled')
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - name: Install runtime libraries
        run: |
          sudo apt-get update
          sudo apt-get install -y apparmor-utils libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 \\
            libcups2t64 libgbm1 libgtk-3-0t64 libnss3 libx11-xcb1 libxcomposite1 \\
            libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 unzip
      - name: Download same-run ARM64 bundle
        uses: actions/download-artifact@v4
        with:
          name: ${{ inputs.artifact }}
          path: ${{ runner.temp }}/chromix-native-bundle
      - name: Verify checksum and native launcher
        run: |
          set -euo pipefail
          cd "${RUNNER_TEMP}/chromix-native-bundle"
          test -s chromix-linux-arm64.zip
          grep -E '^[0-9a-fA-F]{64}  chromix-linux-arm64\\.zip$' SHA256SUMS > ARM64.SHA256SUMS
          test "$(wc -l < ARM64.SHA256SUMS)" -eq 1
          sha256sum --check --strict ARM64.SHA256SUMS
          mkdir "${RUNNER_TEMP}/chromix-native-smoke"
          python3 - <<'PY'
          import importlib.util
          import os
          from pathlib import Path
          path = Path(os.environ["GITHUB_WORKSPACE"]) / "sdk/python/chromix/_binary.py"
          spec = importlib.util.spec_from_file_location("chromix_bundle_extract", path)
          module = importlib.util.module_from_spec(spec)
          spec.loader.exec_module(module)
          module._extract_zip(Path("chromix-linux-arm64.zip"), Path(os.environ["RUNNER_TEMP"]) / "chromix-native-smoke")
          PY
          bash "${GITHUB_WORKSPACE}/build/linux/prepare-ci-sandbox.sh" \\
            "${RUNNER_TEMP}/chromix-native-smoke/chromix/chrome"
          python3 "${GITHUB_WORKSPACE}/tools/verify_linux_bundle.py" \\
            --bundle-dir "${RUNNER_TEMP}/chromix-native-smoke/chromix" --arch arm64 --runtime \\
            2>&1 | tee "${RUNNER_TEMP}/chromix-linux-arm64-native-smoke.log"
      - name: Upload native verification diagnostics
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-native-smoke-attempt-${{ github.run_attempt }}
          path: ${{ runner.temp }}/chromix-linux-arm64-native-smoke.log
          if-no-files-found: warn
          retention-days: 14
"""

body = HEADER + "\n".join(job(s) for s in range(1, STAGES + 1)) + "\n" + NATIVE_LINUX_ARM64
OUT.write_text(body, encoding="utf-8", newline="\n")
print(f"wrote {OUT} ({len(body)} bytes, {STAGES} stages)")
