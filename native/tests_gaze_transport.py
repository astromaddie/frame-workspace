#!/usr/bin/env python3
"""Real native WS backchannel under an idle desktop and reconnect.
Diagnostic UV only, never live gaze. No video/VR/device services touched.
"""
import base64,hashlib,json,pathlib,select,socket,struct,subprocess,sys,tempfile,threading,time
binary=str(pathlib.Path(sys.argv[1]).resolve())
jitter='--clock-jitter' in sys.argv
duration=5.3 if jitter else 1.1
def receive(c):
    def exact(n):
        b=b''
        while len(b)<n:
            part=c.recv(n-len(b))
            if not part:raise EOFError()
            b+=part
        return b
    op,n=exact(2);size=n&127
    if size==126:size=struct.unpack('!H',exact(2))[0]
    if size==127:size=struct.unpack('!Q',exact(8))[0]
    mask=exact(4);raw=exact(size)
    return json.loads(bytes(v^mask[i%4] for i,v in enumerate(raw)))
def send(c,m):
    raw=json.dumps(m).encode();n=len(raw)
    c.sendall(bytes([0x81,n if n<126 else 126])+(b'' if n<126 else struct.pack('!H',n))+raw)
with tempfile.TemporaryDirectory(prefix='fw-gaze-') as folder:
    folder=pathlib.Path(folder)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(10)
    (folder/'url').write_text(f'http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test')
    errors=[];counts=[];ages=[];gaps=[];coordinates=set();hostRegressions=[];freshness=[]
    def server():
        try:
            for attempt in range(2):
                c,_=listener.accept();c.settimeout(5)
                request=b''
                while b'\r\n\r\n' not in request:request+=c.recv(1)
                key=next(x.split(b':',1)[1].strip() for x in request.split(b'\r\n') if x.lower().startswith(b'sec-websocket-key:'))
                accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                c.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                nonce=f'session-{attempt}';send(c,{'t':'hello','r':'reconnect','foveation':1,'direct':1,'gazeSession':nonce})
                pending=None;clock=False;start=time.monotonic();lastStamp=0;lastHost=0;lastArrival=None;count=0;pingCount=0;regressions=0
                while time.monotonic()-start<duration:
                    now=time.monotonic()
                    if pending and now>=pending['due']:
                        send(c,{'t':'pong','c':pending['c'],'a':now*1e6 if pending['lateStamp'] else pending['stamp']})
                        pending=None;clock=True
                    if not select.select([c],[],[],.003)[0]:continue
                    m=receive(c)
                    if m['t']=='ping':
                        pingCount+=1
                        delay=(.06 if pingCount==1 else 0 if pingCount==2 else .24) if jitter else .02
                        pending={'c':m['c'],'due':time.monotonic()+delay,'stamp':time.monotonic()*1e6,
                                 'lateStamp':not jitter or pingCount<3 or attempt==1}
                    if m['t']=='gaze':
                        assert clock and m['session']==nonce and m['version']==1,m
                        if not m['valid']:continue
                        assert m['source']=='diagnostic' and isinstance(m['sampleTime'],int) and m['sampleTime']>lastStamp
                        if lastHost and m['time']<=lastHost:regressions+=1
                        assert 0<=m['u']<=1 and 0<=m['v']<=1
                        coordinates.add((m['u'],m['v']))
                        # The intentionally delayed pong midpoint shifts clock
                        # offset; measure network age using arrival-vs-sample deltas.
                        arrival=time.monotonic()*1000
                        if lastArrival is not None:
                            gaps.append(arrival-lastArrival)
                            ages.append(abs((arrival-lastArrival)-(m['sampleTime']-lastStamp)/1000))
                        freshness.append(arrival-m['time']/1000)
                        assert -50<=freshness[-1]<=150,freshness[-1]
                        lastStamp=m['sampleTime'];lastHost=m['time'];lastArrival=arrival;count+=1
                assert count>=(150 if jitter else 20),count
                counts.append(count);hostRegressions.append(regressions)
                if attempt==1:
                    send(c,{'t':'close'})
                    try:
                        while True:receive(c)
                    except (EOFError,ConnectionError,socket.timeout):pass
                c.close()
        except Exception as e:errors.append(repr(e))
        finally:listener.close()
    t=threading.Thread(target=server);t.start()
    cmd=[binary,'--no-vr','--foveation','--diagnostic-gaze','scan' if '--scan' in sys.argv else '0.2,0.8','--url-file',str(folder/'url'),'--seconds','13' if jitter else '5']
    if '--use-display' not in sys.argv:cmd=['xvfb-run','-a',*cmd]
    run=subprocess.run(cmd,capture_output=True,text=True,timeout=18 if jitter else 10);t.join(5)
    assert not errors and not t.is_alive(),(errors,run.stderr)
    assert run.returncode==1 and 'decoded=0 uploaded=0' in run.stderr,run.stderr
    assert (len(coordinates)>2) if '--scan' in sys.argv else (len(coordinates)==1)
    if jitter:assert all(value>=1 for value in hostRegressions),hostRegressions
    assert max(gaps)<150 and max(ages)<150,(gaps,ages)
    print(json.dumps({'result':'PASS','clockJitter':jitter,'hostTimestampRegressions':hostRegressions,'hostAgeRangeMs':[round(min(freshness),2),round(max(freshness),2)],'movingDiagnostic':('--scan' in sys.argv),'idleGazeMessagesPerConnection':counts,'maxInterarrivalMs':round(max(gaps),2),
                     'maxSampleDeltaVersusArrivalDeltaMs':round(max(ages),2),
                     'coverage':'no video traffic; newest unique timestamps; <=60Hz; monotonic sample ordering despite host-clock correction; bounded freshness under queued pongs; fresh nonce+clock handshake on reconnect; no OpenVR'}))
