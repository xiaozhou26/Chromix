"""Executable snapshot/display contracts; full Chromium integration is separate."""
from pathlib import Path
import os
import shutil
import subprocess
import pytest
from test_fingerprint_config import config_binary  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def snapshot_binary(config_binary):
    root = config_binary.parent
    source = root / 'snapshot-main.cc'
    source.write_text(r'''
#include "base/uxr_config.h"
#include <cassert>
#include <iostream>
#include <thread>
#include <vector>
int main(int argc, char** argv) {
  auto& c = base::UxrConfig::GetInstance();
  if (argc == 2 && std::string(argv[1]) == "freeze") {
    assert(!c.IsInitialized()); assert(!c.SetAll({}, 2));
    assert(!c.IsInitialized()); assert(c.SetAll({{"typed", "7"}}));
    auto copy = c.Snapshot(); copy["typed"] = "8";
    std::vector<std::thread> threads;
    for (int i=0;i<8;++i) threads.emplace_back([&] {
      for (int j=0;j<100;++j) {
        assert(c.SetAll({{"typed", "7"}}));
        assert(!c.SetAll({{"typed", "8"}})); assert(c.Get("typed") == "7");
      }
    });
    for (auto& t : threads) t.join();
    assert(c.IsInitialized()); assert(!c.GetInt("typed", nullptr));
    assert(!c.GetDouble("typed", nullptr)); assert(!c.GetUint64("typed", nullptr));
    std::cout << "frozen"; return 0;
  }
  base::flat_map<std::string, std::string> values;
  for (int i=1;i<argc;++i) {
    std::string item(argv[i]); auto split = item.find('=');
    values[item.substr(0, split)] = item.substr(split + 1);
  }
  bool accepted = c.SetAll(values); auto d = c.Display();
  std::cout << accepted << ' ' << c.IsInitialized() << ' ' << d.width << ' '
            << d.height << ' ' << d.available_width << ' ' << d.available_height
            << ' ' << d.window_x << ' ' << d.window_y << ' '
            << d.device_scale_factor << ' ' << d.orientation_angle;
  if (!accepted) {
    assert(!c.ValidationError().empty()); assert(c.Snapshot().empty());
    assert(c.SetAll({}));
  }
}
''', encoding='utf-8')
    output = root / 'snapshot-test'
    compiler = os.environ.get('CXX') or shutil.which('clang++') or shutil.which('c++')
    result = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                             '-I', str(root), str(root / 'base/uxr_config.cc'), str(source),
                             '-o', str(output)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def call(binary, *args):
    return subprocess.check_output([str(binary), *args], text=True, timeout=15).split()


def test_atomic_snapshot_is_immutable(snapshot_binary):
    assert call(snapshot_binary, 'freeze') == ['frozen']


BASE = ['uxr-screen-width=1920', 'uxr-screen-height=1080']


@pytest.mark.parametrize('extra,expected', [
    ([], ['1920', '1080', '1920', '1080', '0', '0', '0', '0']),
    (['uxr-window-x=-1920', 'uxr-window-y=-100', 'uxr-taskbar-height=40',
      'uxr-device-pixel-ratio=1.25'], ['1920', '1080', '1920', '1040', '-1920', '-100', '1.25', '0']),
    (['uxr-screen-avail-width=0', 'uxr-screen-avail-height=0'],
     ['1920', '1080', '0', '0', '0', '0', '0', '0']),
    (['uxr-screen-orientation=landscape-secondary', 'uxr-screen-orientation-angle=180'],
     ['1920', '1080', '1920', '1080', '0', '0', '0', '180']),
])
def test_backend_geometry(snapshot_binary, extra, expected):
    assert call(snapshot_binary, *BASE, *extra) == ['1', '1', *expected]


@pytest.mark.parametrize('extra', [
    ['uxr-screen-width=0'], ['uxr-screen-width=1920.5'], ['uxr-screen-width=32769'],
    ['uxr-screen-avail-width=1921'], ['uxr-screen-avail-height=1081'],
    ['uxr-taskbar-height=1081'], ['uxr-taskbar-height=40', 'uxr-screen-avail-height=1050'],
    ['uxr-device-pixel-ratio=nan'], ['uxr-device-pixel-ratio=inf'],
    ['uxr-device-pixel-ratio=0.1'], ['uxr-device-pixel-ratio=9'],
    ['uxr-window-x=-1000001'], ['uxr-screen-orientation-angle=65536'],
    ['uxr-screen-orientation=portrait-primary', 'uxr-screen-orientation-angle=45'],
    ['uxr-screen-orientation=invalid'], ['uxr-screen-extended=true'],
    ['uxr-screen-color-depth=30'], ['uxr-viewport-width=100'], ['uxr-outer-height=500'],
])
def test_invalid_geometry_never_publishes_partial_state(snapshot_binary, extra):
    assert call(snapshot_binary, *BASE, *extra) == ['0'] * 10


def test_partial_and_oversized_snapshots(snapshot_binary):
    for args in (['uxr-screen-width=1920'], ['uxr-screen-avail-width=0'], ['BAD=1'],
                 ['typed=' + 'a' * 16385]):
        assert call(snapshot_binary, *args) == ['0'] * 10


def test_getters_no_longer_override_native_layout():
    for number in ('0012', '0018', '0037', '0055', '0056', '0058'):
        added = '\n'.join(line for line in next((ROOT / 'patches').glob(number + '-*.patch')).read_text().splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        assert 'GetInstance()' not in added
        assert 'ScreenMetricsEmulator' in added
    screen = (ROOT / 'patches/0125-display-emulation-backend.patch').read_text()
    widget = (ROOT / 'patches/0126-display-widget-initialization.patch').read_text()
    assert 'screen_infos.assign(1, current)' in screen
    assert 'gfx::Size(display.available_width,' in screen
    assert 'ForTopMostMainFrame() && !uxr_display_initialized_' in widget
    assert 'display.enabled() && !AutoResizeMode() && !DeviceEmulator()' in widget
    assert 'last_web_exposed_screen_infos_' in widget
    assert 'remote_frame->DidChangeScreenInfos(web_screen_infos)' in widget
    assert 'current.is_extended = false' in screen
    assert 'EnableDeviceEmulation(params,' in widget


@pytest.mark.parametrize('number,relative', [
    ('0125', 'third_party/blink/renderer/core/frame/screen_metrics_emulator.cc'),
    ('0126', 'third_party/blink/renderer/core/frame/web_frame_widget_impl.cc'),
    ('0127', 'third_party/blink/renderer/core/frame/web_frame_widget_impl.h'),
    ('0128', 'third_party/blink/renderer/core/frame/web_remote_frame_impl.cc'),
])
def test_patches_on_read_only_native_source(tmp_path, number, relative):
    supplied = Path(os.environ.get('CHROMIX_DISPLAY_BASELINE_ROOT', ROOT / '.chromix-build-win/src'))
    original = supplied / relative
    if not original.is_file():
        pytest.skip('matching pre-Chromix source is unavailable')
    before = (original.read_bytes(), original.stat().st_mtime_ns)
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(before[0])
    patch = next((ROOT / 'patches').glob(number + '-*.patch'))
    for reverse in (False, True):
        command = ['git', '-c', 'core.autocrlf=false', 'apply', '--no-index', '--whitespace=error', str(patch)]
        if reverse:
            command.insert(4, '--reverse')
        result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding='utf-8')
        assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_bytes() == before[0]
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
