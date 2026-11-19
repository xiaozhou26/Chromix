#!/usr/bin/env python3
"""Generate exact ARM stage2 donor continuation at logical stages3 through8."""
from pathlib import Path

try:
    from . import gen_posix_workflow as base
except ImportError:
    import gen_posix_workflow as base

OUT = Path(".github/workflows/build-linux157-arm64-locale-stages.yml")


def render():
    header = base.HEADER.replace("name: build-posix-github", "name: build-linux157-arm64-locale-stages")
    header = header.replace("Restore only compiled x64 run44578881741", "Restore only compiled ARM stage2 run44578888695")
    jobs = []
    for stage in range(3, base.STAGES + 1):
        text = base.job(stage, first_stage=3)
        text = text.replace("(prepare + first compile)", "(resume exact stage2 compiled tree)")
        text = text.replace("Guard exact Linux157 compiled locale recovery", "Guard exact Linux157 ARM stage2 locale recovery")
        text = text.replace(" = linux:x64:fast:false", " = linux:arm64:fast:false")
        text = text.replace("--phase restore --arch x64", "--phase restore --arch arm64")
        text = text.replace("--phase checkpoint --arch x64", "--phase checkpoint --arch arm64")
        text = text.replace('verify_linux157_upload.py --workdir "$WORK" --arch x64',
                            'verify_linux157_upload.py --workdir "$WORK" --arch arm64')
        text = text.replace("Restore latest compiled Linux157 locale checkpoint", "Restore exact ARM stage2 compiled locale checkpoint")
        needle = "          CHROMIX_LINUX157_LOCALE_RECOVERY: ${{ inputs.linux157_locale_recovery && '1' || '0' }}"
        text = text.replace(needle, needle + "\n          CHROMIX_LINUX157_ARM64_STAGE2_RECOVERY: '1'")
        # Even a manual reusable-workflow caller must match the fixed ARM-only route.
        guard = '''      - name: Require fixed ARM stage2-to-stage3 continuation
        run: |
          set -euo pipefail
          test '${{ github.repository }}:${{ github.event_name }}:${{ github.ref }}' = xiaozhou26/Chromix:workflow_dispatch:refs/heads/main
          test '${{ inputs.platform }}:${{ inputs.arch }}:${{ inputs.runner }}:${{ inputs.artifact }}' = linux:arm64:ubuntu-24.04:chromix-linux-arm64
          test '${{ inputs.build_profile }}:${{ inputs.use_upstream_cache }}:${{ inputs.linux157_locale_recovery }}' = fast:false:true
          test '${{ inputs.linux157_pre_gn_recovery }}:${{ inputs.linux157_post_gn_recovery }}' = false:false
          test '${{ inputs.resume_migration }}:${{ inputs.resume_run_id }}:${{ inputs.resume_artifact_ids }}' = ::
          test '${{ inputs.resume_attempt }}:${{ inputs.max-stages }}' = 1:8

'''
        text = text.replace("      - uses: actions/checkout@v4\n\n", "      - uses: actions/checkout@v4\n\n" + guard, 1)
        jobs.append(text)
    native = base.NATIVE_LINUX_ARM64.replace(
        "needs: [posix-1, posix-2, posix-3, posix-4, posix-5, posix-6, posix-7, posix-8]",
        "needs: [posix-3, posix-4, posix-5, posix-6, posix-7, posix-8]")
    return header + "\n".join(jobs) + "\n" + native


def main():
    body = render()
    OUT.write_text(body, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(body)} bytes; logical stages3-8)")


if __name__ == "__main__":
    main()
