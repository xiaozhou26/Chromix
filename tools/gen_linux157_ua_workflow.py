#!/usr/bin/env python3
"""Generate the independent latest x64 UA-header checkpoint recovery workflow."""
from pathlib import Path

try:
    from . import gen_posix_workflow as base
except ImportError:
    import gen_posix_workflow as base

OUT = Path('.github/workflows/build-linux157-x64-ua-stages.yml')


def render():
    body = base.HEADER + '\n'.join(base.job(s) for s in range(1, base.STAGES + 1)) + '\n' + base.NATIVE_LINUX_ARM64
    body = body.replace('name: build-posix-github', 'name: build-linux157-x64-ua-stages')
    body = body.replace('linux157_locale_recovery', 'linux157_ua_recovery')
    body = body.replace('CHROMIX_LINUX157_LOCALE_RECOVERY', 'CHROMIX_LINUX157_UA_RECOVERY')
    body = body.replace('tools/linux157_locale.py', 'tools/linux157_ua.py')
    body = body.replace('linux157-locale-', 'linux157-ua-')
    body = body.replace('run44578881741', 'run44630942999')
    body = body.replace('reviewed0236/0237 locale additions (226 to228)', 'reviewed0238 UA header addition (228 to229)')
    body = body.replace('compiled locale recovery', 'compiled UA recovery')
    body = body.replace('compiled Linux157 locale checkpoint', 'compiled Linux157 UA checkpoint')
    guard = '''      - name: Require exact latest x64 UA recovery route
        run: |
          set -euo pipefail
          test '${{ github.repository }}:${{ github.event_name }}:${{ github.ref }}' = xiaozhou26/Chromix:workflow_dispatch:refs/heads/main
          test '${{ inputs.platform }}:${{ inputs.arch }}:${{ inputs.runner }}:${{ inputs.artifact }}' = linux:x64:ubuntu-22.04:chromix-linux-x64
          test '${{ inputs.linux157_ua_recovery }}:${{ inputs.linux157_pre_gn_recovery }}:${{ inputs.linux157_post_gn_recovery }}' = true:false:false
          test '${{ inputs.build_profile }}:${{ inputs.use_upstream_cache }}:${{ inputs.max-stages }}' = fast:false:8
          test '${{ inputs.resume_run_id }}:${{ inputs.resume_artifact_ids }}:${{ inputs.resume_migration }}' = ::
          test '${{ inputs.resume_attempt }}' = 1

'''
    body = body.replace('      - uses: actions/checkout@v4\n\n', '      - uses: actions/checkout@v4\n\n' + guard)
    return body


def main():
    body = render()
    OUT.write_text(body, encoding='utf-8', newline='\n')
    print(f'wrote {OUT} ({len(body)} bytes)')


if __name__ == '__main__':
    main()
