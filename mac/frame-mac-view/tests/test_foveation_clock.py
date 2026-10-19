#!/usr/bin/env python3
"""Real Session: clock correction, monotonic replay, freshness and reconnect.
Only synthetic fixed gaze and one static TestSource picture; no display/VR.
"""
import http.client,json,os,pathlib,select,subprocess,sys,time
root=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'tests'))
from test_macview import WS
key='foveation-clock-test'
env={**os.environ,'FRAME_MAC_VIEW_TOKEN':key,'FRAME_WORKSPACE_FOVEATION':'1',
     'FRAME_MAC_VIEW_DIAGNOSTIC_GAZE':'1','FRAME_MAC_VIEW_TEST_STATIC':'1','FRAME_MAC_VIEW_ADAPT':'0'}
proc=subprocess.Popen([sys.argv[1],'serve','--port','0','--exit-on-eof'],env=env,
                      stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
ws=None
try:
    port=int(proc.stdout.readline().rsplit(':',1)[1])
    def api(method,path):
        c=http.client.HTTPConnection('127.0.0.1',port);c.request(method,path)
        r=c.getresponse();assert r.status==200
        data=json.loads(r.read());c.close();return data
    def counters():
        stream=max(api('GET',f'/stats?k={key}&settle=0')['streams'],key=lambda s:s['id'])
        return stream['summary']['gaze']
    ticket=api('POST',f'/ticket?k={key}&src=test')['ticket']
    reconnect=None;oldNonce=None;results=[]
    for attempt in range(2):
        credential=('t='+ticket) if attempt==0 else ('r='+reconnect)
        ws=WS(port,'/stream?src=test&'+credential+'&max=640&fps=30&codec=h264&foveation=1');assert ws.status==101
        nonce=None;offset=None;start=time.monotonic();lastGaze=0;lastPing=0;lastSample=0
        duration=2.6 if attempt==0 else .9
        while time.monotonic()-start<duration:
            now=time.monotonic()
            if now-lastPing>.1:
                lastPing=now;ws.send_text(json.dumps({'t':'ping','c':now*1e6}))
            if nonce and offset is not None and now-lastGaze>.02:
                lastGaze=now
                # Reset the sender clock on a new nonce, and force benign
                # backward/forward corrections of synchronized host time.
                lastSample=int((now-start)*1e6)+1
                correction=20_000 if int((now-start)/.6)%2==0 else -20_000
                ws.send_text(json.dumps({'t':'gaze','session':nonce,'version':1,'source':'diagnostic','valid':True,
                                        'u':.5,'v':.5,'time':now*1e6+offset+correction,'sampleTime':lastSample}))
            if not ws.buf and not select.select([ws.s],[],[],.003)[0]:continue
            op,raw=ws.recv()
            if op==1:
                m=json.loads(raw)
                if m['t']=='hello':
                    nonce=m['gazeSession'];reconnect=m['r'];assert nonce!=oldNonce
                    ws.send_text('{"t":"ack"}')
                if m['t']=='pong' and offset is None:offset=m['a']-(m['c']+time.monotonic()*1e6)/2
                assert m['t'] not in ('error','foveation-status'),m['t']
        state=counters();count=state['counts']
        assert state['active'] and count['activations']==1 and count['fallbacks']==0,state
        assert all(count[k]==0 for k in ('replay','stale','future','sampleGap','expired','malformed')),state
        results.append({'accepted':count['accepted'],'activations':count['activations'],'fallbacks':count['fallbacks']})
        if attempt==0:
            def gaze(stamp,delta=0,session=None):
                ws.send_text(json.dumps({'t':'gaze','session':session or nonce,'version':1,'source':'diagnostic','valid':True,
                                        'u':.5,'v':.5,'time':time.monotonic()*1e6+offset+delta,'sampleTime':stamp}))
            gaze(lastSample) # fresh host time must not bypass duplicate-sample rejection
            gaze(lastSample+1,-160_000)
            gaze(lastSample+2,60_000)
            gaze(lastSample+3,session='wrong-session')
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                count=counters()['counts']
                if all(count[k]==1 for k in ('replay','stale','future','session')):break
                time.sleep(.01)
            assert all(count[k]==1 for k in ('replay','stale','future','session')),count
        else:
            # An actual producer stall still expires at150ms, independently of
            # replay ordering or the clock filter.
            time.sleep(.25)
            state=counters()
            assert not state['active'] and state['counts']['expired']==1 and state['counts']['fallbacks']==1,state
        oldNonce=nonce;ws.close();ws=None
    print(json.dumps({'result':'PASS','sessions':results,
                      'coverage':'real Session clock corrections without fallback; duplicate monotonic sample rejected; unchanged stale/future bounds; fresh nonce and ordering on reconnect; actual250ms stall falls back'}))
finally:
    if ws:ws.close()
    proc.stdin.close()
    try:proc.wait(5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
    proc.stdout.close()
