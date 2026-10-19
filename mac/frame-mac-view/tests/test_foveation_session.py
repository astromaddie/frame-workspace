#!/usr/bin/env python3
"""Real localhost companion session; static TestSource, no display capture/VR.
Usage: python3 .../test_foveation_session.py /tmp/frame-mac-view
"""
import http.client,json,os,pathlib,select,subprocess,sys,time
root=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'tests'))
from test_macview import WS
binary=sys.argv[1]
atlas="--atlas" in sys.argv
capability="&atlas=1" if atlas else ""
activeMode=2 if atlas else 1
env={**os.environ,'FRAME_MAC_VIEW_TOKEN':'foveation-test','FRAME_WORKSPACE_FOVEATION':'1',
     'FRAME_MAC_VIEW_DIAGNOSTIC_GAZE':'1','FRAME_MAC_VIEW_TEST_STATIC':'1','FRAME_MAC_VIEW_ADAPT':'0'}
proc=subprocess.Popen([binary,'serve','--port','0','--exit-on-eof'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
ws=None
try:
    port=int(proc.stdout.readline().rsplit(':',1)[1])
    def ticket():
        c=http.client.HTTPConnection('127.0.0.1',port);c.request('POST','/ticket?k=foveation-test&src=test');r=c.getresponse();assert r.status==200
        result=json.loads(r.read())['ticket'];c.close();return result
    def connect(path):
        result=WS(port,path);assert result.status==101;return result
    # A browser/older native viewer retains the exact baseline framing even
    # when the companion process has enabled the negotiated extension.
    ws=connect('/stream?src=test&t='+ticket()+'&max=640&fps=30&codec=h264')
    while True:
        op,data=ws.recv()
        if op==1 and json.loads(data)['t']=='hello':assert 'foveation' not in json.loads(data)
        if op==2:assert not data[0]&0x80 and data[17:21]==b'\0\0\0\1';break
    ws.close()
    ws=connect('/stream?src=test&t='+ticket()+'&max=640&fps=30&codec=h264&foveation=1'+capability)
    nonce=None;reconnect=None;offset=None;frames=[];start=time.monotonic();lastGaze=0;lastPing=0
    while time.monotonic()-start<5.7:
        elapsed=time.monotonic()-start
        if time.monotonic()-lastPing>.1:
            lastPing=time.monotonic();ws.send_text(json.dumps({'t':'ping','c':lastPing*1e6}))
        if nonce and offset is not None and time.monotonic()-lastGaze>.035 and .3<elapsed<5.0:
            lastGaze=time.monotonic()
            valid=not (2.0<elapsed<2.3)
            ws.send_text(json.dumps({'t':'gaze','session':nonce,'version':1,'source':'diagnostic','valid':valid,
                                    'u':.5 if elapsed<1.4 else .05,'v':.5 if elapsed<1.4 else .95,
                                    'time':time.monotonic()*1e6+offset}))
        if not ws.buf and not select.select([ws.s],[],[],.005)[0]:continue
        op,data=ws.recv()
        if op==1:
            m=json.loads(data)
            if m['t']=='hello':
                assert m['foveation']==1 and (m.get('atlas')==1)==atlas;nonce=m['gazeSession'];reconnect=m['r'];ws.send_text('{"t":"ack"}')
            if m['t']=='pong':offset=m['a']-(m['c']+time.monotonic()*1e6)/2
            assert m['t'] not in ('error','foveation-status'),m
        elif op==2:
            assert data[0]&0x80 and data[17]==1
            import struct
            layout=struct.unpack('!8H',data[21:37]);mode=data[18];seq=struct.unpack('!I',data[9:13])[0]
            frames.append((elapsed,mode,layout,seq));ws.send_text(json.dumps({'t':'rx','s':seq}))
            assert data[37:41]==b'\0\0\0\1'
    def any_frame(lo,hi,mode):return any(lo<t<hi and m==mode for t,m,_,_ in frames)
    assert any_frame(0,.5,0),frames
    assert any_frame(.7,1.4,activeMode),frames
    assert any_frame(1.4,2.0,activeMode),frames
    assert any_frame(2.0,2.5,0),frames
    assert not any_frame(2.5,4.0,activeMode),frames
    assert any_frame(4.0,5.0,activeMode),frames
    assert any_frame(5.0,5.5,0),frames
    warped=[l for _,m,l,_ in frames if m]
    assert len({l[4:6] for l in warped})>=2,frames
    assert len({l[2:4] for l in warped})==1,frames
    # Exactly one captured source picture; every subsequent output was the
    # production Session's bounded newest-picture refresh, including expiry.
    c=http.client.HTTPConnection('127.0.0.1',port);c.request('GET','/stats?k=foveation-test&settle=0');streams=json.loads(c.getresponse().read())['streams'];c.close()
    assert streams and all(s['captured']==1 for s in streams),[(s['captured']) for s in streams]
    for stream in streams:
        assert len({frame['cap'] for frame in stream['frames']})==1,stream['frames']
    ws.close();ws=connect('/stream?src=test&r='+reconnect+'&max=640&fps=30&codec=h264&foveation=1'+capability)
    newNonce=None
    while True:
        op,data=ws.recv()
        if op==1 and json.loads(data)['t']=='hello':newNonce=json.loads(data)['gazeSession']
        if op==2:
            assert data[18]==0 and newNonce!=nonce;break
    print(json.dumps({'result':'PASS','mode':activeMode,'coverage':'live Session+Metal BGRA+VT: legacy framing, gaze-only motion on one static capture, invalid/stale full fallback, stable encoded size, cooldown, fresh reconnect nonce/full quality','frames':len(frames)}))
finally:
    if ws:ws.close()
    proc.stdin.close()
    try:proc.wait(5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
    proc.stdout.close()

# Matched fixed/moving gaze at the exact same capture cadence. A healthy
# capture producer must carry gaze updates itself, without repeated captures
# spending the shared submission budget. Frame capture/arrival identities are
# original production statistics, not a synthetic duplicate counter.
import math,struct
env['FRAME_MAC_VIEW_TEST_STATIC']='0'
results=[]
for fps in [30,60]:
    captureFps=27 if fps==30 else 55
    env["FRAME_MAC_VIEW_TEST_CAPTURE_FPS"]=str(captureFps)
    for moving in [False,True]:
        proc=subprocess.Popen([binary,'serve','--port','0','--exit-on-eof'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        ws=None
        try:
            port=int(proc.stdout.readline().rsplit(':',1)[1])
            ws=connect('/stream?src=test&t='+ticket()+f'&max=640&fps={fps}&codec=h264&foveation=1'+capability)
            nonce=None;offset=None;start=time.monotonic();lastGaze=0;lastPing=0;frames=[]
            duration=3.0
            while time.monotonic()-start<duration:
                now=time.monotonic()
                if now-lastPing>.1:
                    lastPing=now;ws.send_text(json.dumps({'t':'ping','c':now*1e6}))
                if nonce and offset is not None and now-lastGaze>.008:
                    lastGaze=now
                    ws.send_text(json.dumps({'t':'gaze','session':nonce,'version':1,'source':'diagnostic','valid':True,
                                            'u':.5+.3*math.sin(now*3) if moving else .5,'v':.5,'time':now*1e6+offset}))
                if not ws.buf and not select.select([ws.s],[],[],.002)[0]:continue
                op,data=ws.recv()
                if op==1:
                    m=json.loads(data)
                    if m['t']=='hello':nonce=m['gazeSession']
                    if m['t']=='pong':offset=m['a']-(m['c']+time.monotonic()*1e6)/2
                    assert m['t'] not in ('error','foveation-status'),m
                elif op==2:
                    seq=struct.unpack('!I',data[9:13])[0]
                    frames.append((now-start,seq,data[18]))
                    ws.send_text(json.dumps({'t':'rx','s':seq}))
            c=http.client.HTTPConnection('127.0.0.1',port);c.request('GET','/stats?k=foveation-test&settle=0');stream=json.loads(c.getresponse().read())['streams'][0];c.close()
            measured={seq for elapsed,seq,mode in frames if elapsed>=.8 and mode==activeMode}
            records=[f for f in stream['frames'] if f['s'] in measured]
            unique=len({(f['cap'],f['arr']) for f in records})
            repeats=len(records)-unique
            assert len(records)>=fps and repeats==0,{'fps':fps,'moving':moving,'frames':len(records),'repeatedCaptures':repeats}
            assert len(frames)<=math.ceil(duration*fps)+2,(fps,len(frames))
            assert abs(stream['captured']-duration*captureFps)<=3,(captureFps,stream['captured'])
            results.append({'fps':fps,'captureFps':captureFps,'moving':moving,'captured':stream['captured'],'frames':len(frames),'measuredUnique':unique,'repeatedCaptures':repeats})
            # Even while capture is healthy, explicit loss must restore full
            # quality on the next paced picture; it must not await quiet gating.
            lost=time.monotonic();ws.send_text(json.dumps({'t':'gaze','session':nonce,'version':1,'source':'diagnostic','valid':False}))
            while True:
                op,data=ws.recv()
                if op==2:
                    ws.send_text(json.dumps({'t':'rx','s':struct.unpack('!I',data[9:13])[0]}))
                    if data[18]==0:break
                assert time.monotonic()-lost<.15,'loss did not restore full quality promptly'
            assert time.monotonic()-lost<.15,'full fallback waited for capture quiet'
        finally:
            if ws:ws.close()
            proc.stdin.close()
            try:proc.wait(5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            proc.stdout.close()
for fixed,moving in zip(results[::2],results[1::2]):
    assert abs(fixed['captured']-moving['captured'])<=3,(fixed,moving)
    assert moving['measuredUnique']>=fixed['measuredUnique']-3,(fixed,moving)
print(json.dumps({'result':'PASS','coverage':'matched30/60fps fixed versus moving gaze: no repeated-capture submissions, comparable unique capture delivery, nominal pacing and prompt loss fallback','runs':results}))
