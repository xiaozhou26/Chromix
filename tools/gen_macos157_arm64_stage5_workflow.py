#!/usr/bin/env python3
"""Generate the fixed mac157 stage5 route without changing the generic CI recipe."""
from pathlib import Path

try:
    from . import gen_posix_workflow as base
except ImportError:
    import gen_posix_workflow as base

ROOT = Path(__file__).resolve().parents[1]
OUT = '.github/workflows/build-mac157-arm64-stage5-stages.yml'
STAGE = 'build/posix/ci-stage-mac157-arm64-stage5.sh'


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('shared stage integration point changed: ' + old)
    return text.replace(old, new, 1)


def render_stage():
    text = (ROOT / 'build/posix/ci-stage.sh').read_text()
    text = replace_once(text, '  mac156-arm64-hevc) UNCHANGED_HELPER=',
                        '  mac157-arm64-stage5) UNCHANGED_HELPER="$REPO/tools/migrate_macos157_arm64_stage5.py" ;;\n'
                        '  mac156-arm64-hevc) UNCHANGED_HELPER=')
    text = replace_once(text, 'if [ "${CHROMIX_UNCHANGED_RESUME:-}" = mac156-arm64-stage8 ] ||',
                        'if [ "${CHROMIX_UNCHANGED_RESUME:-}" = mac157-arm64-stage5 ] ||\n'
                        '   [ "${CHROMIX_UNCHANGED_RESUME:-}" = mac156-arm64-stage8 ] ||')
    text = replace_once(text, 'elif [ -e "$WORK/fingerprint-diagnostics/mac156-x64-stage3-resume.json" ] ||',
                        'elif [ -e "$WORK/fingerprint-diagnostics/mac157-arm64-stage5-resume.json" ] ||\n'
                        '     [ -e "$WORK/fingerprint-diagnostics/mac156-x64-stage3-resume.json" ] ||')
    text = replace_once(text, 'if [ "$RC" -ne 0 ]; then\n  die "build script failed',
                        'if [ "$RC" -ne 0 ]; then\n'
                        '  if [ "${CHROMIX_UNCHANGED_RESUME:-}" = mac157-arm64-stage5 ]; then\n'
                        '    bash "$REPO/build/posix/ci-parts.sh" "$WORK" "$SNAPSHOT_DIR"\n'
                        '    emit upload_snapshot true\n'
                        '  fi\n  die "build script failed')
    text = replace_once(text, '  if [ -z "$UNCHANGED_HELPER" ] && { [[ "$CHROMIUM_VERSION_PIN" = 157.* ]] ||',
                        '  if { [ -z "$UNCHANGED_HELPER" ] ||\n'
                        '       [ "${CHROMIX_UNCHANGED_RESUME:-}" = mac157-arm64-stage5 ]; } &&\n'
                        '     { [[ "$CHROMIUM_VERSION_PIN" = 157.* ]] ||')
    guard = '''[ "$PLATFORM:$ARCH:$MAX_STAGES:${CHROMIX_UNCHANGED_RESUME:-}" = macos:arm64:8:mac157-arm64-stage5 ] ||
  { echo "fixed mac157 ARM64 stage5 route required" >&2; exit 2; }
'''
    text = replace_once(text, 'mkdir -p "$WORK"\n', guard + '\nmkdir -p "$WORK"\n')
    return text


def render():
    header = base.HEADER.replace('name: build-posix-github', 'name: build-mac157-arm64-stage5-stages')
    guard = '''      - name: Require exact mac157 ARM64 stage5 route
        run: |
          set -euo pipefail
          test '${{ github.repository }}:${{ github.event_name }}:${{ github.ref }}' = xiaozhou26/Chromix:workflow_dispatch:refs/heads/main
          test '${{ inputs.platform }}:${{ inputs.arch }}:${{ inputs.runner }}' = macos:arm64:macos-26
          test '${{ inputs.artifact }}:${{ inputs.max-stages }}' = chromix-mac157-arm64-stage5-resume:8
          test '${{ inputs.resume_migration }}:${{ inputs.resume_run_id }}:${{ inputs.resume_tree_stage }}:${{ inputs.resume_attempt }}:${{ inputs.resume_artifact_ids }}' = mac157-arm64-stage5:44567777209:5:1:14626292942
          test '${{ inputs.build_profile }}:${{ inputs.use_upstream_cache }}:${{ inputs.macos_compile_release }}' = fast:true:true
          test '${{ inputs.linux157_pre_gn_recovery }}:${{ inputs.linux157_post_gn_recovery }}:${{ inputs.linux157_locale_recovery }}' = false:false:false

'''
    jobs = []
    for stage in range(1, 9):
        text = base.job(stage)
        for old, new in (
            ('build/posix/ci-stage.sh', STAGE),
            ('mac156-arm64-hevc', 'mac157-arm64-stage5'),
            ('mac156-arm64-stage3-resume', 'mac157-arm64-stage5-resume'),
            ('tools/migrate_macos156_hevc_snapshot.py', 'tools/migrate_macos157_arm64_stage5.py'),
            ('42703307545:3:1:13748238839,13748243740', '44567777209:5:1:14626292942'),
            ('fixed mac156 HEVC source migration', 'fixed mac157 ICU/UA source migration'),
            ('exact mac156 ARM64 HEVC migration inputs', 'exact mac157 ARM64 ICU/UA migration inputs'),
        ):
            text = text.replace(old, new)
        text = replace_once(text, '      - uses: actions/checkout@v4\n\n',
                            '      - uses: actions/checkout@v4\n\n' + guard)
        jobs.append(text)
    return header + '\n'.join(jobs)


def main():
    (ROOT / OUT).write_text(render())
    path = ROOT / STAGE
    path.write_text(render_stage())
    path.chmod(0o755)
    print('wrote fixed mac157 ARM64 stage5 workflow and isolated stage entry')


if __name__ == '__main__':
    main()
