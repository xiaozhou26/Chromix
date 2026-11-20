#!/usr/bin/env python3
"""Generate exact ARM stage3 UA-donor continuation at logical stages4 through8."""
from pathlib import Path

try:
    from . import gen_posix_workflow as base
except ImportError:
    import gen_posix_workflow as base

OUT = Path('.github/workflows/build-linux157-arm64-ua-stages.yml')


def render():
    header = base.HEADER.replace('name: build-posix-github', 'name: build-linux157-arm64-ua-stages')
    header = header.replace('linux157_locale_recovery', 'linux157_ua_recovery')
    header = header.replace('Restore only compiled x64 run44578881741', 'Restore only compiled ARM stage3 run44639133090')
    header = header.replace('reviewed0236/0237 locale additions (226 to228)', 'reviewed0238 UA header addition (228 to229)')
    jobs = []
    for stage in range(4, base.STAGES + 1):
        text = base.job(stage, first_stage=4)
        text = text.replace('(prepare + first compile)', '(resume exact stage3 compiled tree)')
        text = text.replace('linux157_locale_recovery', 'linux157_ua_recovery')
        text = text.replace('CHROMIX_LINUX157_LOCALE_RECOVERY', 'CHROMIX_LINUX157_UA_RECOVERY')
        text = text.replace('tools/linux157_locale.py', 'tools/linux157_ua.py')
        text = text.replace('linux157-locale-', 'linux157-ua-')
        text = text.replace('Guard exact Linux157 compiled locale recovery', 'Guard exact ARM stage3 UA recovery')
        text = text.replace(' = linux:x64:fast:false', ' = linux:arm64:fast:false')
        text = text.replace('--phase restore --arch x64', '--phase restore --arch arm64')
        text = text.replace('--phase checkpoint --arch x64', '--phase checkpoint --arch arm64')
        text = text.replace('verify_linux157_upload.py --workdir "$WORK" --arch x64',
                            'verify_linux157_upload.py --workdir "$WORK" --arch arm64')
        text = text.replace('Restore latest compiled Linux157 locale checkpoint', 'Restore exact ARM stage3 compiled UA checkpoint')
        needle = "          CHROMIX_LINUX157_UA_RECOVERY: ${{ inputs.linux157_ua_recovery && '1' || '0' }}"
        text = text.replace(needle, needle + "\n          CHROMIX_LINUX157_ARM64_UA_STAGE3_RECOVERY: '1'")
        guard = '''      - name: Require fixed ARM stage3-to-stage4 UA continuation
        run: |
          set -euo pipefail
          test '${{ github.repository }}:${{ github.event_name }}:${{ github.ref }}' = xiaozhou26/Chromix:workflow_dispatch:refs/heads/main
          test '${{ inputs.platform }}:${{ inputs.arch }}:${{ inputs.runner }}:${{ inputs.artifact }}' = linux:arm64:ubuntu-24.04:chromix-linux-arm64
          test '${{ inputs.build_profile }}:${{ inputs.use_upstream_cache }}:${{ inputs.linux157_ua_recovery }}' = fast:false:true
          test '${{ inputs.linux157_pre_gn_recovery }}:${{ inputs.linux157_post_gn_recovery }}' = false:false
          test '${{ inputs.resume_migration }}:${{ inputs.resume_run_id }}:${{ inputs.resume_artifact_ids }}' = ::
          test '${{ inputs.resume_attempt }}:${{ inputs.max-stages }}' = 1:8

'''
        text = text.replace('      - uses: actions/checkout@v4\n\n', '      - uses: actions/checkout@v4\n\n' + guard, 1)
        jobs.append(text)
    native = base.NATIVE_LINUX_ARM64.replace(
        'needs: [posix-1, posix-2, posix-3, posix-4, posix-5, posix-6, posix-7, posix-8]',
        'needs: [posix-4, posix-5, posix-6, posix-7, posix-8]')
    return header + '\n'.join(jobs) + '\n' + native


def main():
    body = render()
    OUT.write_text(body, encoding='utf-8', newline='\n')
    print(f'wrote {OUT} ({len(body)} bytes; logical stages4-8)')


if __name__ == '__main__':
    main()
