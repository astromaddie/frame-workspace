#!/usr/bin/env python3
"""Loopback integration of the production WebSocket -> H264 -> GL path.
Requires ffmpeg with libx264, Xvfb and the built native binary. No headset.
"""
import base64,hashlib,json,os,pathlib,socket,struct,subprocess,sys,tempfile,threading,time
binary=pathlib.Path(sys.argv[1]).resolve()
expect_no_v4l2='--expect-no-v4l2' in sys.argv[2:]

def receive(conn):
    def exact(n):
        b=b''
        while len(b)<n:
            part=conn.recv(n-len(b))
            if not part: raise EOFError()
            b+=part
        return b
    a,b=exact(2); n=b&127
    if n==126:n=struct.unpack('!H',exact(2))[0]
    elif n==127:n=struct.unpack('!Q',exact(8))[0]
    assert b&128, 'client must mask messages'
    mask=exact(4);raw=exact(n)
    return a&15,bytes(v^mask[i%4] for i,v in enumerate(raw))

def send(conn,payload,op=1):
    n=len(payload)
    header=bytes([0x80|op,n]) if n<126 else bytes([0x80|op,126])+struct.pack('!H',n) if n<65536 else bytes([0x80|op,127])+struct.pack('!Q',n)
    conn.sendall(header+payload)

with tempfile.TemporaryDirectory(prefix='native-stream-') as td:
    temp=pathlib.Path(td)
    # Exact same color source decoded by ffmpeg becomes the image oracle.
    encoded=subprocess.check_output(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=320x180:r=30','-frames:v','1','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-f','h264','pipe:1'])
    expected=subprocess.check_output(['ffmpeg','-v','error','-f','h264','-i','pipe:0','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],input=encoded)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(15)
    port=listener.getsockname()[1]
    (temp/'url').write_text(f'http://127.0.0.1:{port}/view?src=test&t=one-use-secret&max=4096&fps=30&codec=h264')
    (temp/'control').write_text(json.dumps({'revision':1,'width':3,'distance':2.5,'focus':True}))
    counts={'ack':0,'rx':0,'clock':0,'fd':0,'focus':0}; errors=[]
    reconnect_test='--reconnect' in sys.argv[2:]
    def server():
      try:
        for attempt in range(2 if reconnect_test else 1):
          conn,_=listener.accept();conn.settimeout(10)
          request=b''
          while b'\r\n\r\n' not in request:request+=conn.recv(1)
          assert b'/stream?src=test&codec=h264&max=4096&fps=30' in request
          if attempt==0:assert b'&t=one-use-secret ' in request
          else:assert b'&r=reconnect-secret ' in request and b'one-use-secret' not in request
          key=next(line.split(b':',1)[1].strip() for line in request.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
          accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
          conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
          send(conn,b'{"t":"hello","r":"reconnect-secret"}')
          # Ping interleaved with fragmented JSON exercises RFC6455 continuation.
          conn.sendall(b'\x01\x0b'+b'{"t":"info"')
          send(conn,b'probe',9)
          conn.sendall(b'\x80\x01'+b'}')
          seq=0; last=0; began=time.monotonic()
          while time.monotonic()-began<(1.2 if reconnect_test and attempt==0 else 5):
            now=time.monotonic()
            if now-last>=.05:
              seq+=1;send(conn,b'\x01'+struct.pack('!QII',int(now*1e6),seq,0)+encoded,2);last=now
            conn.settimeout(.01)
            try:
              op,raw=receive(conn)
              if op==10:assert raw==b'probe';continue
              m=json.loads(raw);kind=m['t']
              if kind in counts:counts[kind]+=1
              if kind=='ping':send(conn,json.dumps({'t':'pong','c':m['c'],'a':time.monotonic()*1e6}).encode())
            except socket.timeout:pass
            except (EOFError,ConnectionResetError):break
          conn.close()
      except Exception as exc:errors.append(repr(exc))
    thread=threading.Thread(target=server);thread.start()
    proc=subprocess.run(['xvfb-run','-a',str(binary),'--no-vr','--decoder','v4l2' if expect_no_v4l2 else 'software','--url-file',str(temp/'url'),'--seconds','4','--dump',str(temp/'frame.ppm'),'--state-file',str(temp/'state'),'--control-file',str(temp/'control'),'--pose-file',str(temp/'pose')],capture_output=True,text=True,timeout=15,env={**os.environ,'LIBGL_ALWAYS_SOFTWARE':'1'})
    thread.join();listener.close()
    if expect_no_v4l2:
        state=json.loads((temp/'state').read_text())
        assert proc.returncode==1 and not errors,(proc.stdout,proc.stderr,errors)
        assert state['decoderRequested']=='v4l2' and state['decoderActive']=='',state
        assert state['decoderCodec']=='h264_v4l2m2m' and state['decoderError']!=0,state
        assert state['decoderErrorStage']=='open' and state['uploaded']==0,state
        assert state['received']>=1 and state['decoderInputWidth']==320 and state['decoderInputHeight']==180,state
        assert state['decoderCodedWidth']==320 and state['decoderCodedHeight']==192,state
        assert state['exitReason']=='decoder-error' and 'no fallback' in proc.stdout+proc.stderr,state
        print(json.dumps({'v4l2_unavailable_fail_closed':'passed','initialization':'after actual keyframe','parsed_dimensions':[320,180,320,192]}))
        sys.exit(0)
    assert proc.returncode==0,proc.stderr
    assert not errors,errors
    assert 'one-use-secret' not in proc.stderr and 'reconnect-secret' not in proc.stderr
    state=json.loads((temp/'state').read_text());pose=json.loads((temp/'pose').read_text())
    assert state['uploaded']>20 and state['decoded']>20,state
    assert state['textureWidth']==320 and state['textureHeight']==180,state
    assert state['decoderRequested']=='software' and state['decoderActive']=='software' and state['decoderCodec']=='h264',state
    assert state['decoderError']==0 and state['exitReason']=='timer',state
    assert state['errors']==(1 if reconnect_test else 0),state
    assert counts['ack']==(2 if reconnect_test else 1) and counts['rx']>20 and counts['clock']>0 and counts['fd']>0 and counts['focus']==1,counts
    assert pose['width']==3 and pose['distance']==2.5,pose
    raw=(temp/'frame.ppm').read_bytes().split(b'\n',3)[3]
    assert len(raw)==len(expected)
    delta=max(abs(a-b) for a,b in zip(raw,expected));assert delta<=2,delta
    # Restart must retain the saved pose rather than recentering automatically.
    restarted=subprocess.run(['xvfb-run','-a',str(binary),'--no-vr','--test-pattern','--seconds','.2','--pose-file',str(temp/'pose'),'--state-file',str(temp/'restart-state')],capture_output=True,text=True,timeout=10,env={**os.environ,'LIBGL_ALWAYS_SOFTWARE':'1'})
    assert restarted.returncode==0,restarted.stderr
    assert json.loads((temp/'pose').read_text())==pose
    assert json.loads((temp/'restart-state').read_text())['placement']==pose
    # Compact retained evidence only; generated video/dump is removed by TemporaryDirectory.
    report={'test':'production socket/h264/GL integration','forced_reconnect':reconnect_test,'result':'passed','uploaded':state['uploaded'],'decoded':state['decoded'],'errors':state['errors'],'wire_messages':counts,'max_rgb_difference_vs_ffmpeg':delta,'dimensions':[320,180],'headset_test':False,'pose_preserved_on_restart':True}
    print(json.dumps(report,sort_keys=True))
