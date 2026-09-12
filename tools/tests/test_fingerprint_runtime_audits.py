"""Offline validator mutation tests. Fixtures are not browser observations."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_runtime_audit as runtime
import fingerprint_render_audit as render


def runtime_report():
    screen = {'width':1920, 'height':1080, 'availWidth':1920, 'availHeight':1040, 'isExtended':False}
    out = {}
    for name, width, height, scale in [('display_initial',800,600,1), ('display_resized',1024,768,1),
                                      ('display_scaled',1024,768,1.5)]:
        out[name] = {'screen':dict(screen), 'dpr':1.25, 'window':{'innerWidth':width, 'innerHeight':height},
                     'layout':{'width':width, 'height':height},
                     'viewport':{'width':width / scale, 'height':height / scale, 'scale':scale},
                     'css':dict.fromkeys(('width','deviceWidth','deviceHeight','resolution'), True)}
    out.update(screens={'status':'observed', 'screens':[{'primary':True, 'width':1920, 'height':1080, 'dpr':1.25}]},
        display_oopif={'is_oopif':True, 'geometry':{'screen':screen, 'dpr':1.25,
            'window':{'innerWidth':300, 'innerHeight':150}, 'layout':{'width':300, 'height':150},
            'css':dict.fromkeys(('width','deviceWidth','deviceHeight','resolution'), True)}},
        timing={'samples':[{'raf':i, 'now':i + 0.1, 'wall':1000+i, 'origin':1000} for i in range(6)],
                'dst':['01:59','03:00','01:59','01:00']}, lifecycle={'before':1, 'after':2},
        audio={'sampleRate':44100, 'frames':4096, 'channels':1, 'peak':0.5, 'mutable':True},
        media_denied={'error':'NotAllowedError', 'devices':[]},
        media_capture={'settings':{'width':160, 'height':120},
            'frame':{'codedWidth':320, 'codedHeight':240, 'displayWidth':160, 'displayHeight':120,
                     'visible':{'width':320, 'height':240}},
            'capabilities':{'width':{'min':1, 'max':1920}, 'height':{'min':1, 'max':1080}},
            'decoded':{'width':320, 'height':240}, 'live':{'width':160, 'height':120},
            'mime':'video/webm;codecs=vp8,opus', 'bytes':123, 'playable':'probably'},
        media_restart={'original':[['videoinput','device-a','group-a']],
            'same_document':[['videoinput','device-a','group-a']],
            'reloaded':[['videoinput','device-a','group-b']],
            'other_origin':[['videoinput','device-b','group-c']]})
    return {'schema_version':1, 'configuration_mode':'launch-backend', 'observations':out, 'errors':[]}


def test_runtime_fixture_and_document_salted_groups():
    report = runtime_report()
    assert runtime.assess(report) == []
    restart = report['observations']['media_restart']
    assert runtime.media_identity_errors(restart) == []
    restart['reloaded'] = deepcopy(restart['original'])
    assert 'rotate' in str(runtime.media_identity_errors(restart))


@pytest.mark.parametrize('name', runtime_report()['observations'])
def test_missing_runtime_path_fails(name):
    report = runtime_report(); del report['observations'][name]
    assert runtime.assess(report)


@pytest.mark.parametrize('path,value', [
    (('display_initial','dpr'), 1), (('display_resized','layout','width'), 800),
    (('display_scaled','viewport','scale'), 1), (('display_scaled','css','width'), False),
    (('display_oopif','is_oopif'), False), (('display_oopif','geometry','dpr'), 1),
    (('display_oopif','geometry','window','innerWidth'), 240),
    (('display_oopif','geometry','layout','width'), 240),
    (('display_oopif','geometry','css','resolution'), False),
    (('display_initial','screen','availHeight'), 1080),
    (('media_capture','decoded','width'), 160), (('media_capture','settings','width'), 99),
    (('media_capture','bytes'), 0), (('media_denied','error'), None),
    (('media_denied','devices'), [{'deviceId':'leak'}]), (('audio','mutable'), False),
    (('audio','peak'), float('nan')), (('lifecycle','after'), 0), (('timing','dst'), []),
    (('media_restart','other_origin'), [['videoinput','device-a','group-c']]),
])
def test_runtime_cross_path_mismatch_fails(path, value):
    report = runtime_report(); target = report['observations']
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert runtime.assess(report)


def render_report():
    gpu = {'status':'observed', 'features':['shader-f16'], 'enabledFeatures':['shader-f16'],
           'limits':{'maxBufferSize':1024, 'minUniformBufferOffsetAlignment':256},
           'boundaries':[{'name':'maxBufferSize', 'advertised':1024, 'requested':1025, 'rejected':True, 'accepted':1024},
                         {'name':'minUniformBufferOffsetAlignment', 'advertised':256, 'requested':128, 'rejected':True, 'accepted':256}]}
    return {'schema_version':1, 'errors':[], 'unavailable':[], 'observed':{
        'bitmap':dict.fromkeys(('crop','resize','flip','bitmaprenderer','ownership'), True),
        'workerOwnership':{'detached':True, 'pixels':True},
        'exports':[{'kind':kind, 'snapshots':3, 'sourceMutation':True} for kind in ('html','offscreen')],
        **{api:{'status':'observed', 'contextRestored':True, 'before':[64,128,191,255]*4,
                'after':[64,128,191,255]*4} for api in ('webgl','webgl2')}, 'webgpu':gpu}}


def test_render_fixture_and_explicit_unavailability():
    report = render_report()
    assert render.assess(report) == ([], [])
    report['observed']['webgpu'] = {'status':'unavailable'}
    assert render.assess(report) == ([], ['webgpu'])


@pytest.mark.parametrize('name', render_report()['observed'])
def test_missing_render_path_is_not_a_pass(name):
    report = render_report(); del report['observed'][name]
    assert render.assess(report)[0]


@pytest.mark.parametrize('mutate', [
    lambda r: r['bitmap'].update(ownership=False),
    lambda r: r['webgl'].update(after=[0]*16),
    lambda r: r['webgl2'].update(contextRestored=False),
    lambda r: r['webgpu'].update(enabledFeatures=[]),
    lambda r: r['webgpu']['boundaries'].pop(),
    lambda r: r['webgpu']['boundaries'][0].update(accepted=1023),
    lambda r: r['webgpu']['boundaries'][1].update(accepted=512),
    lambda r: r['webgpu']['boundaries'][0].update(rejected=False),
    lambda r: r['webgpu']['boundaries'][0].update(requested=1024),
])
def test_render_wrong_pixels_features_and_limit_boundaries(mutate):
    report = render_report(); mutate(report['observed'])
    assert render.assess(report)[0]


@pytest.mark.parametrize('bad', [None, [], {'observations':None}, {'schema_version':1,'observed':None}])
def test_malformed_evidence_returns_failures_not_tracebacks(bad):
    assert runtime.assess(bad)
    assert render.assess(bad)[0]
