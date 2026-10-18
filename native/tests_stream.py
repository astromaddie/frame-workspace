#!/usr/bin/env python3
"""Loopback integration of the production WebSocket -> H264 -> GL path.
Requires ffmpeg with libx264 and the built native binary. Uses Xvfb by default;
`--use-display` runs on the existing display. No headset.
"""
import base64,hashlib,json,os,pathlib,socket,struct,subprocess,sys,tempfile,threading,time
binary=pathlib.Path(sys.argv[1]).resolve()
options=sys.argv[2:]
expect_no_v4l2='--expect-no-v4l2' in options
reconnect_test='--reconnect' in options
size_change='--size-change' in options
use_display='--use-display' in options
conversion='gpu' if '--conversion' in options and options[options.index('--conversion')+1]=='gpu' else 'cpu'
case=options[options.index('--case')+1] if '--case' in options else 'default'
duration=float(options[options.index('--duration')+1]) if '--duration' in options else 4.0

if case not in {'default','bt601-limited','bt601-full','bt709-limited','bt709-full','unsupported-444'}:
    raise SystemExit('unknown conversion test case')
if not 0.8 <= duration <= 30:
    raise SystemExit('invalid test duration')

def encode(case_name, size):
    width,height=size
    source=f'testsrc2=size={width}x{height}:rate=30' if case_name!='default' else f'color=c=red:s={width}x{height}:r=30'
    args=['ffmpeg','-v','error','-f','lavfi','-i',source,'-frames:v','1']
    if case_name!='default':
        colorspace='bt709' if '709' in case_name else 'smpte170m'
        color_range='pc' if case_name.endswith('full') else 'tv'
        args += ['-colorspace',colorspace,'-color_range',color_range]
        if case_name=='unsupported-444':
            args += ['-pix_fmt','yuv444p','-profile:v','high444']
        else:
            args += ['-pix_fmt','yuv420p']
    args += ['-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-f','h264','pipe:1']
    encoded=subprocess.check_output(args)
    expected=subprocess.check_output(['ffmpeg','-v','error','-f','h264','-i','pipe:0','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],input=encoded)
    return encoded,expected

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
    # Decode the exact encoded access unit with FFmpeg for an image oracle.
    encoded,expected=encode(case,(320,180))
    resized,resized_expected=encode(case,(640,360)) if size_change else (encoded,expected)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(15)
    port=listener.getsockname()[1]
    (temp/'url').write_text(f'http://127.0.0.1:{port}/view?src=test&t=one-use-secret&max=4096&fps=30&codec=h264')
    (temp/'control').write_text(json.dumps({'revision':1,'width':3,'distance':2.5,'focus':True}))
    counts={'ack':0,'rx':0,'clock':0,'fd':0,'focus':0}; errors=[]
    sent_sequences=set();received_sequences=set();frame_deliveries=[]
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
          while time.monotonic()-began<(1.2 if reconnect_test and attempt==0 else max(duration,1.2)):
            now=time.monotonic()
            if now-last>=.05:
              seq+=1
              use_resized=size_change and now-began>=.8
              payload=resized if use_resized else encoded
              packet_seq=101+(seq-1) if use_resized else seq
              sent_sequences.add(packet_seq)
              send(conn,b'\x01'+struct.pack('!QII',int(now*1e6),packet_seq,0)+payload,2);last=now
            conn.settimeout(.01)
            try:
              op,raw=receive(conn)
              if op==10:assert raw==b'probe';continue
              m=json.loads(raw);kind=m['t']
              if kind in counts:counts[kind]+=1
              if kind=='rx':received_sequences.add(int(m['s']))
              if kind=='fd':frame_deliveries.extend(m['f'])
              if kind=='ping':send(conn,json.dumps({'t':'pong','c':m['c'],'a':time.monotonic()*1e6}).encode())
            except socket.timeout:pass
            except (EOFError,ConnectionResetError):break
          # Let the viewer own normal shutdown. Closing at the same deadline
          # as its timer creates an unrelated WebSocket error-count race.
          # Attempt zero in the forced-reconnect case still closes below.
          if not reconnect_test or attempt > 0:
            conn.settimeout(.05)
            until=time.monotonic()+5
            while time.monotonic()<until:
              try:
                op,raw=receive(conn)
                if op==10:continue
                m=json.loads(raw);kind=m['t']
                if kind in counts:counts[kind]+=1
                if kind=='rx':received_sequences.add(int(m['s']))
                if kind=='fd':frame_deliveries.extend(m['f'])
                if kind=='ping':send(conn,json.dumps({'t':'pong','c':m['c'],'a':time.monotonic()*1e6}).encode())
              except socket.timeout:pass
              except (EOFError,ConnectionResetError):break
          conn.close()
      except Exception as exc:errors.append(repr(exc))
    thread=threading.Thread(target=server);thread.start()
    dump_filter=['--dump-size','640x360'] if size_change else []
    command=[str(binary),'--no-vr','--decoder','v4l2' if expect_no_v4l2 else 'software','--conversion',conversion,'--url-file',str(temp/'url'),'--seconds',str(duration),'--dump',str(temp/'frame.ppm'),*dump_filter,'--state-file',str(temp/'state'),'--control-file',str(temp/'control'),'--pose-file',str(temp/'pose')]
    launcher=[] if use_display else ['xvfb-run','-a']
    test_env=os.environ.copy()
    if not use_display:test_env['LIBGL_ALWAYS_SOFTWARE']='1'
    proc=subprocess.run(launcher+command,capture_output=True,text=True,timeout=max(15,duration+10),env=test_env)
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
    # Classify retries separately so decoder and submission errors cannot hide
    # inside the aggregate counter.
    categorized_errors={name:state[name] for name in
                        ('networkErrors','serverErrors','decoderSendErrors',
                         'decoderReceiveErrors','decoderFatalErrors','submitErrors')}
    assert sum(categorized_errors.values())==state['errors'],(state,categorized_errors)
    min_frames=20 if case=='default' and duration>=4 else 8
    assert state['uploaded']>min_frames and state['decoded']>min_frames,state
    expected_size=[640,360] if size_change else [320,180]
    assert [state['textureWidth'],state['textureHeight']]==expected_size,state
    assert state['decoderRequested']=='software' and state['decoderActive']=='software' and state['decoderCodec']=='h264',state
    assert state['decoderError']==0 and state['exitReason']=='timer',state
    assert state['conversionRequested']==conversion,state
    if conversion=='gpu' and case!='unsupported-444':
        assert state['conversionActive']=='gpu' and state['conversionFallbacks']==0,state
        assert state['decoderPixelFormat'] in (0,12),state  # YUV420P or YUVJ420P
        expected_range=2 if case.endswith('full') else 1
        assert state['decoderColorRange']==expected_range,state
        assert state['planeCopyMs']>0 and state['convertMs']>=state['planeCopyMs'],state
    if case=='unsupported-444':
        assert state['conversionActive']=='cpu-fallback' and state['conversionFallbacks']>0,state
        assert state['decoderPixelFormat']!=0,state
    if conversion=='cpu': assert state['planeCopyMs']==0,state
    if reconnect_test:
        assert state['networkErrors']==1 and state['errors']==1,(state,proc.stderr)
        assert not (state['serverErrors'] or state['decoderSendErrors'] or
                    state['decoderReceiveErrors'] or state['decoderFatalErrors'] or
                    state['submitErrors']),(state,proc.stderr)
    elif size_change:
        assert state['networkErrors']<=1 and state['errors']==state['networkErrors'],(state,proc.stderr)
        assert not (state['serverErrors'] or state['decoderSendErrors'] or
                    state['decoderReceiveErrors'] or state['decoderFatalErrors'] or
                    state['submitErrors']),(state,proc.stderr)
    else:
        assert state['networkErrors']==0 and state['errors']==0,(state,proc.stderr)
    assert counts['ack']==(2 if reconnect_test else 1) and counts['rx']>min_frames and counts['clock']>0 and counts['fd']>0 and counts['focus']==1,counts
    assert received_sequences and received_sequences<=sent_sequences,(received_sequences,sent_sequences)
    assert frame_deliveries and all(
        len(row)==4 and int(row[0]) in received_sequences and
        float(row[1])>0 and float(row[2])>=float(row[1]) and int(row[3])==0
        for row in frame_deliveries
    ),frame_deliveries
    assert pose['width']==3 and pose['distance']==2.5,pose
    raw=(temp/'frame.ppm').read_bytes().split(b'\n',3)[3]
    oracle=resized_expected if size_change else expected
    assert len(raw)==len(oracle)
    delta=max(abs(a-b) for a,b in zip(raw,oracle))
    if conversion=='cpu': assert delta<=2,delta
    else:
        if delta>4:
            worst=max(range(len(raw)),key=lambda i:abs(raw[i]-oracle[i]))
            pixel=worst//3
            x,y=pixel%expected_size[0],pixel//expected_size[0]
            raise AssertionError({'gpu_rgb_delta':delta,'pixel':[x,y],'channel':worst%3,
                                  'actual':list(raw[pixel*3:pixel*3+3]),
                                  'expected':list(oracle[pixel*3:pixel*3+3]),
                                  'state':state})
    # Restart must retain the saved pose rather than recentering automatically.
    restarted=subprocess.run(launcher+[str(binary),'--no-vr','--test-pattern','--seconds','.2','--pose-file',str(temp/'pose'),'--state-file',str(temp/'restart-state')],capture_output=True,text=True,timeout=10,env=test_env)
    assert restarted.returncode==0,restarted.stderr
    assert json.loads((temp/'pose').read_text())==pose
    assert json.loads((temp/'restart-state').read_text())['placement']==pose
    # Compact retained evidence only; generated video/dump is removed by TemporaryDirectory.
    report={'test':'production socket/h264/GL integration','conversion':conversion,'case':case,'forced_reconnect':reconnect_test,'size_change':size_change,'result':'passed','uploaded':state['uploaded'],'decoded':state['decoded'],'convert_ms':state['convertMs'],'plane_copy_ms':state['planeCopyMs'],'gpu_convert_ms':state['gpuConvertMs'],'errors':state['errors'],'error_reasons':{**categorized_errors,'decoder_error':state['decoderError'],'decoder_error_stage':state['decoderErrorStage'],'exit_reason':state['exitReason']},'wire_messages':counts,'reported_frames':len(frame_deliveries),'sequence_timing_validated':True,'max_rgb_difference_vs_ffmpeg':delta,'dimensions':expected_size,'headset_test':False,'pose_preserved_on_restart':True}
    print(json.dumps(report,sort_keys=True))
