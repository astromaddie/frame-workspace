#!/usr/bin/env python3
"""Own disposable non16:9 display, real production Session/SCK/VT/WebSocket.
Usage: test_direct_session.py CANDIDATE HOST_PROBE [EVIDENCE_DIRECTORY]
No installed companion, Workspace display, user window or headset mutation.
"""
import http.client,json,os,pathlib,select,struct,subprocess,sys,time
root=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'tests'))
from test_macview import WS
binary,probe=sys.argv[1:3]
evidence=pathlib.Path(sys.argv[3]) if len(sys.argv)>3 else None
processes=[]; ws=None

def launch(enabled):
    env={**os.environ,'FRAME_MAC_VIEW_TOKEN':'direct-local-test','FRAME_WORKSPACE_FOVEATION':str(int(enabled)),
         'FRAME_MAC_VIEW_DIAGNOSTIC_GAZE':'1','FRAME_MAC_VIEW_ADAPT':'0'}
    p=subprocess.Popen([binary,'serve','--port','0','--exit-on-eof'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    processes.append(p)
    return int(p.stdout.readline().rsplit(':',1)[1])
def connect(port,src,flags):
    result=WS(port,'/stream?k=direct-local-test&src='+src+'&max=644&fps=30&codec=h264'+flags)
    assert result.status==101
    return result
def rejected(port,src,flags):
    s=connect(port,src,flags); saw_error=False
    try:
        for _ in range(6):
            op,data=s.recv()
            assert op!=2,'unsupported request emitted video'
            if op==1:
                message=json.loads(data); assert message['t']!='hello',message
                saw_error |= message['t']=='error'
            if op==8: break
        assert saw_error,'missing explicit direct error'
    finally:s.close()
try:
    disabled=launch(False)
    rejected(disabled,'display:1','&foveation=1&direct=1')
    ws=connect(disabled,'test','')
    while True:
        op,data=ws.recv()
        if op==1 and json.loads(data)['t']=='hello':assert 'direct' not in json.loads(data)
        if op==2:assert not data[0]&0x80;break
    ws.close();ws=None
    port=launch(True)
    for src in ['test','window:1','separate:1']:
        rejected(port,src,'&foveation=1&direct=1')
    rejected(port,'display:4294967295','&foveation=1&direct=1')
    fixture=subprocess.Popen([probe,'--hold'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    processes.append(fixture)
    line=fixture.stdout.readline();assert line.startswith('DISPLAY '),line
    src='display:'+line.split()[1]
    ws=connect(port,src,'&foveation=1&direct=1')
    nonce=None;offset=None;frames=[];sequence=bytearray();start=time.monotonic();last_gaze=last_ping=0
    while time.monotonic()-start<7:
        elapsed=time.monotonic()-start
        if time.monotonic()-last_ping>.1:
            last_ping=time.monotonic();ws.send_text(json.dumps({'t':'ping','c':last_ping*1e6}))
        if nonce and offset is not None and time.monotonic()-last_gaze>.02 and .3<elapsed<5.5:
            last_gaze=time.monotonic()
            valid=not (2.2<elapsed<2.5)
            # Non-centre, continuously changing valid eye requests.
            import math
            ws.send_text(json.dumps({'t':'gaze','session':nonce,'version':1,'source':'diagnostic','valid':valid,
                'u':.5+.24*math.sin(elapsed*3),'v':.5+.24*math.cos(elapsed*2),
                'time':time.monotonic()*1e6+offset,'sampleTime':int(time.monotonic()*1e6)}))
        if not ws.buf and not select.select([ws.s],[],[],.005)[0]:continue
        op,data=ws.recv()
        if op==1:
            m=json.loads(data)
            if m['t']=='hello':
                assert m['foveation']==1 and m['direct']==1 and 'atlas' not in m,m
                nonce=m['gazeSession'];ws.send_text('{"t":"ack"}')
            if m['t']=='pong':offset=m['a']-(m['c']+time.monotonic()*1e6)/2
            assert m['t'] not in ('error','closed','foveation-status'),m
        elif op==2:
            assert nonce is not None,'frame before supported-path hello'
            assert data[0]&0x80 and data[17:21]==bytes([1,3,0,0]),data[:37]
            layout=struct.unpack('!8H',data[21:37]);seq=struct.unpack('!I',data[9:13])[0]
            assert layout[:4]==(644,484,644,484),layout
            assert layout[6:]==(322,242),layout
            assert data[37:41]==b'\0\0\0\1'
            frames.append((elapsed,layout,seq))
            sequence.extend(struct.pack('!I',len(data)));sequence.extend(data)
            ws.send_text(json.dumps({'t':'rx','s':seq}))
    centre=(162,122)
    def centre_in(lo,hi):return any(lo<t<hi and l[4:6]==centre for t,l,_ in frames)
    assert frames and frames[0][1][4:6]==centre,frames
    assert centre_in(2.2,3.0),frames
    assert centre_in(5.65,7),frames
    assert len({l[4:6] for t,l,_ in frames if .9<t<2.2})>3,frames
    assert len({l[4:6] for t,l,_ in frames if 4.6<t<5.5})>1,frames
    c=http.client.HTTPConnection('127.0.0.1',port);c.request('GET','/stats?k=direct-local-test');r=c.getresponse();status=json.loads(r.read());c.close()
    report={'result':'PASS','frames':len(frames),'distinctCropPositions':len({l[4:6] for _,l,_ in frames}),
        'bytes':len(sequence),'durationSeconds':7,'geometry':[644,484],'mode':3,
        'checks':['feature-off direct rejection','unsupported source rejection','startup failure rejection','legacy off unchanged',
                  'direct hello after actual capture','fixed wire and encoded dimensions','no-gaze centre','invalid centre','expired centre','reacquisition'],
        'stats':status}
    # Diagnostic counters only; strip per-frame/input records and no gaze UVs.
    report['stats']=[{'summary':s.get('summary',{})} for s in status.get('streams',[])] if isinstance(status,dict) else []
    if evidence:
        evidence.mkdir(parents=True,exist_ok=True)
        (evidence/'direct-host.sequence').write_bytes(sequence)
        (evidence/'direct-session.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,sort_keys=True))
finally:
    if ws:ws.close()
    for p in reversed(processes):
        if p.stdin:p.stdin.close()
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=5)
