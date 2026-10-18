#!/usr/bin/env python3
"""Loopback integration of the production WebSocket -> H264 -> GL path.
Requires ffmpeg with libx264 and the built native binary. Uses Xvfb by default;
`--use-display` runs on the existing display. No headset.
"""
import base64,hashlib,json,os,pathlib,socket,struct,subprocess,sys,tempfile,threading,time
import re
binary=pathlib.Path(sys.argv[1]).resolve()
options=sys.argv[2:]
expect_no_v4l2='--expect-no-v4l2' in options
reconnect_test='--reconnect' in options
size_change='--size-change' in options
overload_test='--overload' in options
decoder_option=None
if '--decoder' in options:
    if options.index('--decoder')+1>=len(options):
        raise SystemExit('--decoder requires software or v4l2')
    decoder_option=options[options.index('--decoder')+1]
hardware_decoder=expect_no_v4l2 or overload_test or decoder_option=='v4l2'
use_display='--use-display' in options
conversion='gpu' if '--conversion' in options and options[options.index('--conversion')+1]=='gpu' else 'cpu'
case=options[options.index('--case')+1] if '--case' in options else 'default'
duration=float(options[options.index('--duration')+1]) if '--duration' in options else (8.0 if overload_test else 4.0)

if case not in {'default','bt601-limited','bt601-full','bt709-limited','bt709-full','unsupported-444'}:
    raise SystemExit('unknown conversion test case')
if overload_test and (reconnect_test or size_change or expect_no_v4l2):
    raise SystemExit('--overload cannot be combined with reconnect, size-change, or unavailable-decoder checks')
if expect_no_v4l2 and '--decoder' in options:
    raise SystemExit('--expect-no-v4l2 selects its decoder itself')
if decoder_option is not None and decoder_option not in {'software','v4l2'}:
    raise SystemExit('decoder must be software or v4l2')
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

def split_access_units(bitstream):
    def find_start(position):
        while position+3<=len(bitstream):
            if bitstream[position:position+4]==b'\x00\x00\x00\x01':return position,4
            if bitstream[position:position+3]==b'\x00\x00\x01':return position,3
            position+=1
        return None
    nals=[];found=find_start(0)
    while found:
        start,prefix=found
        next_start=find_start(start+prefix)
        end=next_start[0] if next_start else len(bitstream)
        nal=bitstream[start:end]
        if len(nal)>prefix:nals.append((nal,nal[prefix]&0x1f))
        found=next_start
    groups=[];current=[];saw_aud=False
    for nal,nal_type in nals:
        if nal_type==9:
            if saw_aud and current:
                groups.append(current);current=[]
            saw_aud=True
        current.append((nal,nal_type))
    if current:groups.append(current)
    units=[(b''.join(nal for nal,_ in group),any(nal_type==5 for _,nal_type in group)) for group in groups]
    if not units or not units[0][1]:
        raise AssertionError({'invalid_access_unit_sequence':len(units)})
    return units

def encode_idr_sequence(case_name, size, frame_count=61):
    width,height=size
    args=['ffmpeg','-v','error','-f','lavfi','-i',f'color=c=red:s={width}x{height}:r=30',
          '-frames:v',str(frame_count)]
    if case_name!='default':
        colorspace='bt709' if '709' in case_name else 'smpte170m'
        color_range='pc' if case_name.endswith('full') else 'tv'
        args += ['-colorspace',colorspace,'-color_range',color_range]
        if case_name=='unsupported-444':args += ['-pix_fmt','yuv444p','-profile:v','high444']
        else:args += ['-pix_fmt','yuv420p']
    else:args += ['-pix_fmt','yuv420p']
    args += ['-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-bf','0',
             '-g','2','-keyint_min','2','-sc_threshold','0',
             '-x264-params','aud=1:repeat-headers=1','-f','h264','pipe:1']
    encoded=subprocess.check_output(args)
    units=split_access_units(encoded)
    idr_count=sum(1 for _,is_idr in units if is_idr)
    if len(units)!=frame_count or idr_count<2 or not units[-1][1]:
        raise AssertionError({'access_units':len(units),'expected_access_units':frame_count,
                              'idr_access_units':idr_count,'last_access_unit_is_idr':units[-1][1] if units else False})
    traced=subprocess.run(['ffmpeg','-hide_banner','-loglevel','verbose','-f','h264','-i','pipe:0',
                           '-c:v','copy','-bsf:v','trace_headers','-f','null','-'],
                          input=encoded,capture_output=True,check=True)
    raw_ids=re.findall(r'idr_pic_id\s+[01]+\s*=\s*(\d+)',traced.stderr.decode('utf-8','replace'))
    idr_ids=[]
    for value in raw_ids:
        if not idr_ids or value!=idr_ids[-1]:idr_ids.append(value)
    if len(idr_ids)!=idr_count or any(a==b for a,b in zip(idr_ids,idr_ids[1:])):
        raise AssertionError({'idr_pic_ids':idr_ids,'idr_access_units':idr_count})
    expected=subprocess.check_output(['ffmpeg','-v','error','-f','h264','-i','pipe:0','-frames:v','1',
                                     '-pix_fmt','rgb24','-f','rawvideo','pipe:1'],input=encoded)
    return units,expected

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
    stream_case='default' if overload_test else case
    v4l2_fixture=decoder_option=='v4l2' and not overload_test
    stream_size=(2880,2560) if overload_test or v4l2_fixture else (320,180)
    resized_size=(2816,2560) if v4l2_fixture else (640,360)
    sequence_units,expected=encode_idr_sequence(stream_case,stream_size) if overload_test or v4l2_fixture else (None,None)
    if sequence_units:encoded=sequence_units[0][0]
    else:encoded,expected=encode(stream_case,stream_size)
    if size_change and v4l2_fixture:
        resized_units,resized_expected=encode_idr_sequence(case,resized_size)
        resized=resized_units[0][0]
    elif size_change:
        resized,resized_expected=encode(case,resized_size)
        resized_units=None
    else:
        resized,resized_expected=encoded,expected
        resized_units=None
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(15)
    port=listener.getsockname()[1]
    (temp/'url').write_text(f'http://127.0.0.1:{port}/view?src=test&t=one-use-secret&max=4096&fps=30&codec=h264')
    (temp/'control').write_text(json.dumps({'revision':1,'width':3,'distance':2.5,'focus':True}))
    counts={'ack':0,'rx':0,'clock':0,'fd':0,'focus':0}; errors=[]
    sent_sequences=set();received_sequences=set();frame_deliveries=[]
    reconnect_test='--reconnect' in sys.argv[2:]
    def server():
      try:
        sequence_cursor=0;resized_cursor=0
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
          if overload_test:
            # A finite valid H.264 sequence floods the queue. Only actual IDR
            # access units carry the key flag; adjacent IDR pic_ids are checked
            # above so recovery cannot rely on replaying one malformed IDR.
            for seq,(payload,is_idr) in enumerate(sequence_units,1):
              now=time.monotonic()
              packet_seq=seq
              sent_sequences.add(packet_seq)
              send(conn,(b'\x01' if is_idr else b'\x00')+struct.pack('!QII',int(now*1e6),packet_seq,0)+payload,2)
          while time.monotonic()-began<(1.2 if reconnect_test and attempt==0 else max(duration,1.2)):
            now=time.monotonic()
            if not overload_test and now-last>=.05:
              seq+=1
              use_resized=size_change and (seq>=16 if v4l2_fixture else now-began>=.8)
              if v4l2_fixture:
                if use_resized:
                  index=resized_cursor;resized_cursor+=1
                  units=resized_units;packet_seq=101+index
                else:
                  index=sequence_cursor;sequence_cursor+=1
                  units=sequence_units;packet_seq=index+1
                if index<len(units):
                  payload,is_idr=units[index]
                  sent_sequences.add(packet_seq)
                  send(conn,(b'\x01' if is_idr else b'\x00')+
                       struct.pack('!QII',int(now*1e6),packet_seq,0)+payload,2)
              else:
                payload=resized if use_resized else encoded
                packet_seq=101+(seq-1) if use_resized else seq
                sent_sequences.add(packet_seq)
                send(conn,b'\x01'+struct.pack('!QII',int(now*1e6),packet_seq,0)+payload,2)
              last=now
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
    dump_filter=['--dump-size','x'.join(map(str,resized_size))] if size_change else []
    command=[str(binary),'--no-vr','--decoder','v4l2' if hardware_decoder else 'software','--conversion',conversion,'--url-file',str(temp/'url'),'--seconds',str(duration),'--dump',str(temp/'frame.ppm'),*dump_filter,'--state-file',str(temp/'state'),'--control-file',str(temp/'control'),'--pose-file',str(temp/'pose')]
    launcher=[] if use_display else ['xvfb-run','-a']
    test_env=os.environ.copy()
    if not use_display:test_env['LIBGL_ALWAYS_SOFTWARE']='1'
    try:
        proc=subprocess.run(launcher+command,capture_output=True,text=True,timeout=max(15,duration+10),env=test_env)
    except subprocess.TimeoutExpired as exc:
        state_path=temp/'state'
        timeout_state=json.loads(state_path.read_text()) if state_path.exists() else None
        def tail(value):
            if isinstance(value,bytes):value=value.decode(errors='replace')
            return value[-3000:] if value else ''
        raise AssertionError({'native_test_timeout_seconds':max(15,duration+10),
                              'state':timeout_state,'stdout_tail':tail(exc.stdout),
                              'stderr_tail':tail(exc.stderr)}) from exc
    thread.join();listener.close()
    if expect_no_v4l2:
        state=json.loads((temp/'state').read_text())
        assert proc.returncode==1 and not errors,(proc.stdout,proc.stderr,errors)
        assert state['decoderRequested']=='v4l2' and state['decoderActive']=='',state
        assert state['decoderCodec']=='h264_v4l2m2m' and state['decoderError']!=0,state
        assert state['decoderErrorStage']=='open' and state['uploaded']==0,state
        assert state['decoderOpenAttempts']==1 and state['decoderInitialOpens']==1 and state['decoderOpenFailures']==1,state
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
    if overload_test:min_frames=0
    assert state['uploaded']>min_frames and state['decoded']>min_frames,state
    expected_size=list(resized_size) if size_change else list(stream_size)
    assert [state['textureWidth'],state['textureHeight']]==expected_size,state
    expected_decoder='v4l2' if hardware_decoder else 'software'
    expected_codec='h264_v4l2m2m' if hardware_decoder else 'h264'
    assert state['decoderRequested']==expected_decoder and state['decoderActive']==expected_decoder and state['decoderCodec']==expected_codec,state
    assert state['decoderError']==0 and state['exitReason']=='timer',state
    assert state['decoderOpenFailures']==0,state
    assert state['conversionRequested']==conversion,state
    if conversion=='gpu' and case!='unsupported-444':
        assert state['conversionActive']=='gpu' and state['conversionFallbacks']==0,state
        expected_range=2 if case.endswith('full') else 1
        if hardware_decoder:
            # V4L2 supplies NV12 directly. Keep raw unspecified color metadata
            # visible in the report; only the BT.601 limited oracle can use the
            # shader's documented limited-range default without claiming that
            # the hardware decoder reported it.
            assert state['decoderPixelFormat']==23,state  # AV_PIX_FMT_NV12
            decoder_range=state['decoderColorRange']
            decoder_space=state['decoderColorSpace']
            assert decoder_range in (0,1,2),state
            if case!='default':
                if decoder_range==0:
                    assert case=='bt601-limited',(case,state)
                else:
                    assert decoder_range==expected_range,state
                expected_space=1 if '709' in case else 6  # BT.709 / SMPTE-170M
                if decoder_space==2:  # AVCOL_SPC_UNSPECIFIED
                    assert case=='bt601-limited',(case,state)
                else:
                    assert decoder_space==expected_space,state
        else:
            # Preserve the existing software YUV420P/YUVJ420P metadata gate.
            assert state['decoderPixelFormat'] in (0,12),state
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
        if hardware_decoder:
            assert state['decoderInitialOpens']==1 and state['decoderReconnectOpens']==1 and state['decoderOpenAttempts']==2,state
    elif size_change:
        assert state['networkErrors']<=1 and state['errors']==state['networkErrors'],(state,proc.stderr)
        assert not (state['serverErrors'] or state['decoderSendErrors'] or
                    state['decoderReceiveErrors'] or state['decoderFatalErrors'] or
                    state['submitErrors']),(state,proc.stderr)
        if hardware_decoder:
            assert state['decoderInitialOpens']==1 and state['decoderFormatReopens']>=1 and state['decoderOpenAttempts']>=2,state
    elif overload_test:
        assert state['networkErrors']==0 and state['errors']==0,(state,proc.stderr)
        assert state['received']>=len(sequence_units),state
        assert state['queueOverflows']>0 and state['overflowRecoveryIdrs']>0,state
        assert state['decoderInitialOpens']==1 and state['decoderReconnectOpens']==0 and state['decoderFormatReopens']==0 and state['decoderOpenAttempts']==1,state
        assert state['decoderTimingHighWater']<=64,state
        recovery_seq=state['lastOverflowRecoverySeq']
        assert recovery_seq>0 and any(int(row[0])>=recovery_seq for row in frame_deliveries),(recovery_seq,frame_deliveries,state)
    elif hardware_decoder:
        assert state['networkErrors']==0 and state['errors']==0,(state,proc.stderr)
        assert state['decoderInitialOpens']==1 and state['decoderReconnectOpens']==0 and state['decoderFormatReopens']==0 and state['decoderOpenAttempts']==1,state
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
    report={'test':'production socket/h264/GL integration','conversion':conversion,'case':case,'forced_reconnect':reconnect_test,'forced_overload':overload_test,'size_change':size_change,'result':'passed','uploaded':state['uploaded'],'decoded':state['decoded'],'convert_ms':state['convertMs'],'plane_copy_ms':state['planeCopyMs'],'gpu_convert_ms':state['gpuConvertMs'],'decoder_pixel_format':state['decoderPixelFormat'],'decoder_color_space':state['decoderColorSpace'],'decoder_color_range':state['decoderColorRange'],'queue_overflows':state['queueOverflows'],'overflow_recovery_idrs':state['overflowRecoveryIdrs'],'last_overflow_recovery_seq':state['lastOverflowRecoverySeq'],'stale_driver_frames_dropped':state['staleDriverFramesDropped'],'decoder_open_attempts':state['decoderOpenAttempts'],'decoder_initial_opens':state['decoderInitialOpens'],'decoder_reconnect_opens':state['decoderReconnectOpens'],'decoder_format_reopens':state['decoderFormatReopens'],'decoder_open_ms_total':state['decoderOpenTotalMs'],'decoder_timing_high_water':state['decoderTimingHighWater'],'errors':state['errors'],'error_reasons':{**categorized_errors,'decoder_error':state['decoderError'],'decoder_error_stage':state['decoderErrorStage'],'exit_reason':state['exitReason']},'wire_messages':counts,'reported_frames':len(frame_deliveries),'sequence_timing_validated':True,'max_rgb_difference_vs_ffmpeg':delta,'dimensions':expected_size,'headset_test':False,'pose_preserved_on_restart':True}
    print(json.dumps(report,sort_keys=True))
