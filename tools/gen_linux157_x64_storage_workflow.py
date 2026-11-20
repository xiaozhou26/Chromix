#!/usr/bin/env python3
"""Generate exact x64 stage1 Storage-donor continuation at logical stages2 through8."""
from pathlib import Path

try:
    from . import gen_posix_workflow as base
except ImportError:
    import gen_posix_workflow as base

OUT = Path('.github/workflows/build-linux157-x64-storage-stages.yml')


def render():
    header = base.HEADER.replace('name: build-posix-github', 'name: build-linux157-x64-storage-stages')
    header = header.replace('linux157_locale_recovery', 'linux157_storage_recovery')
    header = header.replace('Restore only compiled x64 run44578881741', 'Restore only compiled x64 stage1 run44687163614')
    header = header.replace('reviewed0236/0237 locale additions (226 to228)', 'reviewed Storage source repair (old229)')
    dependencies = base.LINUX_CLEAN.replace(
        "      - name: Install Linux build dependencies\n        if: runner.os == 'Linux'\n",
        "      - name: Install Linux build dependencies\n        if: runner.os == 'Linux'\n        timeout-minutes: 20\n",
    ).replace(
        '          sudo apt-get update\n',
        '          APT_OPTIONS=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30\n'
        '                       -o Acquire::Retries=3 -o DPkg::Lock::Timeout=60)\n'
        '          sudo timeout -k 30s 300s apt-get "${APT_OPTIONS[@]}" update -o APT::Update::Error-Mode=any\n',
    ).replace(
        '          sudo apt-get install -y ',
        '          sudo timeout -k 30s 600s apt-get "${APT_OPTIONS[@]}" install -y ',
    )
    jobs = []
    for stage in range(2, base.STAGES + 1):
        text = base.job(stage, first_stage=2)
        text = text.replace(base.LINUX_CLEAN, dependencies)
        text = text.replace('(prepare + first compile)', '(resume exact stage1 compiled tree)')
        text = text.replace('linux157_locale_recovery', 'linux157_storage_recovery')
        text = text.replace('CHROMIX_LINUX157_LOCALE_RECOVERY', 'CHROMIX_LINUX157_X64_STORAGE_RECOVERY')
        text = text.replace('tools/linux157_locale.py', 'tools/linux157_x64_storage.py --command storage')
        text = text.replace('tools/linux157_source.py', 'tools/linux157_x64_storage.py --command source')
        text = text.replace('tools/verify_linux157_upload.py', 'tools/linux157_x64_storage.py --command upload')
        text = text.replace('linux157-locale-', 'linux157-storage-')
        diagnostic = "            ${{ runner.temp }}/chromix-build/linux157-storage-origin/"
        text = text.replace(diagnostic, diagnostic + "\n            ${{ runner.temp }}/chromix-build/linux157-ua-origin/\n            ${{ runner.temp }}/chromix-build/linux157-locale-origin/")
        text = text.replace('Guard exact Linux157 compiled locale recovery', 'Guard exact x64 stage1 Storage recovery')
        text = text.replace('Restore latest compiled Linux157 locale checkpoint', 'Restore exact x64 stage1 compiled Storage checkpoint')
        needle = "          CHROMIX_LINUX157_X64_STORAGE_RECOVERY: ${{ inputs.linux157_storage_recovery && '1' || '0' }}"
        text = text.replace(needle, needle + "\n          CHROMIX_LINUX157_X64_STORAGE_STAGE1_RECOVERY: '1'")
        guard = '''      - name: Require fixed x64 stage1-to-stage2 Storage continuation
        run: |
          set -euo pipefail
          test '${{ github.repository }}:${{ github.event_name }}:${{ github.ref }}' = xiaozhou26/Chromix:workflow_dispatch:refs/heads/main
          test '${{ inputs.platform }}:${{ inputs.arch }}:${{ inputs.runner }}:${{ inputs.artifact }}' = linux:x64:ubuntu-22.04:chromix-linux-x64
          test '${{ inputs.build_profile }}:${{ inputs.use_upstream_cache }}:${{ inputs.linux157_storage_recovery }}' = fast:false:true
          test '${{ inputs.linux157_pre_gn_recovery }}:${{ inputs.linux157_post_gn_recovery }}' = false:false
          test '${{ inputs.resume_migration }}:${{ inputs.resume_run_id }}:${{ inputs.resume_artifact_ids }}' = ::
          test '${{ inputs.resume_attempt }}:${{ inputs.max-stages }}' = 1:8

'''
        text = text.replace('      - uses: actions/checkout@v4\n\n', '      - uses: actions/checkout@v4\n\n' + guard, 1)
        jobs.append(text)
    return (header + '\n'.join(jobs)).rstrip() + '\n'


def main():
    body = render()
    OUT.write_text(body, encoding='utf-8', newline='\n')
    print(f'wrote {OUT} ({len(body)} bytes; logical stages2-8)')


if __name__ == '__main__':
    main()
