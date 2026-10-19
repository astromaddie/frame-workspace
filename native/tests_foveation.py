#!/usr/bin/env python3
"""Production Mac Metal/VT fixtures -> WebSocket -> libavcodec -> GL oracle.
Generate fixtures with mac/frame-mac-view/tests/FoveationTests.swift first.
No VR, no persistent gaze data. Requires ffmpeg and Xvfb on Linux.
"""
import argparse, base64, hashlib, json, pathlib, select, socket, struct, subprocess, tempfile, threading, time
import math

p = argparse.ArgumentParser()
p.add_argument('binary', type=pathlib.Path)
p.add_argument('fixtures', type=pathlib.Path)
p.add_argument('--decoder', choices=['software', 'v4l2'], default='software')
p.add_argument('--conversion', choices=['cpu', 'gpu'], default='cpu')
p.add_argument('--use-display', action='store_true')
p.add_argument('--preview', type=pathlib.Path)
p.add_argument('--case', action='append', help='explicit fixture name(s); default runs every fixture')
p.add_argument('--no-resize', action='store_true', help='start at the post-resize IDR; retains continuous same-encoder gaze motion')
a = p.parse_args()
firstPicture = 3 if a.no_resize else 0
dumpSequence = 10 - firstPicture
frames = json.loads((a.fixtures/'fixtures.json').read_text())
if a.case:
    assert set(a.case)<=set(frame['name'] for frame in frames),'unknown fixture case'
    frames=[frame for frame in frames if frame['name'] in a.case]
sequences={}
for frame in frames:
    raw=(a.fixtures/(frame['name']+'.sequence')).read_bytes();units=[];offset=0
    while offset<len(raw):
        length=struct.unpack('!I',raw[offset:offset+4])[0];offset+=4
        units.append(raw[offset:offset+length]);offset+=length
    assert len(units)==(180 if frame.get("fallbackChain") else 40) and units[0][0]&1
    sequences[frame['name']]=units

def receive(c):
    def exact(n):
        b=b''
        while len(b)<n:
            part=c.recv(n-len(b))
            if not part: raise EOFError()
            b+=part
        return b
    op,n=exact(2); size=n&127
    if size==126: size=struct.unpack('!H',exact(2))[0]
    if size==127: size=struct.unpack('!Q',exact(8))[0]
    mask=exact(4); raw=exact(size)
    return op&15,bytes(v^mask[i%4] for i,v in enumerate(raw))

def send(c, payload, op=1):
    if isinstance(payload,dict): payload=json.dumps(payload).encode()
    n=len(payload)
    c.sendall(bytes([0x80|op, n if n<126 else 126 if n<65536 else 127]) +
              (b'' if n<126 else struct.pack('!H',n) if n<65536 else struct.pack('!Q',n)) + payload)

def mapping(count, start, core):
    return [x*.5 if x<start else start*.5+x-start if x<start+core else start*.5+core+(x-start-core)*.5 for x in (i+.5 for i in range(count))]

def oracle(frame):
    w,h=frame['encodedWidth'],frame['encodedHeight']
    # Dump sequence 10 is the seventh picture of the target reference chain
    # (three preceding pictures belong to the other layout). Decode that exact
    # picture for the oracle rather than assuming every lossy P-frame is equal.
    chain=frame.get('fallbackChain',False)
    data=b''.join(unit[37:] for unit in sequences[frame['name']][60:100]) if chain else b''.join(unit[37:] for unit in sequences[frame['name']][3:10])
    selection='select=eq(n\\,39)' if chain else 'select=eq(n\\,6)'
    xs=mapping(frame['width'],frame['left'],frame['coreWidth'])
    ys=mapping(frame['height'],frame['top'],frame['coreHeight'])
    pix='rgba' if a.conversion=='cpu' else 'yuvj420p' if frame['fullRange'] else 'yuv420p'
    raw=subprocess.check_output(['ffmpeg','-v','error','-f','h264','-i','pipe:0','-vf',selection,'-frames:v','1','-pix_fmt',pix,'-f','rawvideo','pipe:1'],input=data)
    if frame.get('mode')==2 and a.conversion=='cpu':
        # Decode once to planar YUV, then convert each independent tile through
        # FFmpeg. No RGB/chroma resampler may reach across the atlas seam.
        planar='yuvj420p' if frame['fullRange'] else 'yuv420p'
        yuv=subprocess.check_output(['ffmpeg','-v','error','-f','h264','-i','pipe:0','-vf',selection,'-frames:v','1','-pix_fmt',planar,'-f','rawvideo','pipe:1'],input=data)
        tiles=[]
        for tile in range(2):
            cropped=bytearray();offset=0
            for pw,ph in [(w,h),(w//2,h//2),(w//2,h//2)]:
                for row in range(ph):
                    start=offset+row*pw+tile*(pw//2)
                    cropped.extend(yuv[start:start+pw//2])
                offset+=pw*ph
            tiles.append(subprocess.check_output(['ffmpeg','-v','error','-f','rawvideo','-pixel_format',planar,'-video_size',f'{w//2}x{h}',
                         '-colorspace','bt709','-i','pipe:0','-frames:v','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'],input=cropped))
        raw=b''.join(tiles[0][row*w*2:(row+1)*w*2]+tiles[1][row*w*2:(row+1)*w*2] for row in range(h))
    result=bytearray()
    def clipped(v):return min(255,max(0,round(v)))
    for sourceY in range(frame['height']):
        for sourceX in range(frame['width']):
            if frame.get('mode')==2:
                px,py=sourceX+.5,sourceY+.5
                focal=frame['left']<=px<frame['left']+frame['coreWidth'] and frame['top']<=py<frame['top']+frame['coreHeight']
                tile=w//2 if focal else 0
                x=tile+px-frame['left'] if focal else px/2
                y=py-frame['top'] if focal else py/2
                x=min(tile+w//2-.5,max(tile+.5,x));y=min(h-.5,max(.5,y))
            else:x,y=xs[sourceX],ys[sourceY]
            xx=min(w-1,max(0,x-.5));x0=int(xx);x1=min(x0+1,w-1);fx=xx-x0;cx=min(int(x/2),w//2-1)
            if frame.get('mode')==2:
                # Chroma indices are confined to this tile independently of
                # luma's half-texel filtering clamp.
                cx=max(tile//2,min(tile//2+w//4-1,cx))
            yy=min(h-1,max(0,y-.5));y0=int(yy);y1=min(y0+1,h-1);fy=yy-y0;cy=min(int(y/2),h//2-1)
            def bilinear(channel=0,channels=1):
                v00=raw[(y0*w+x0)*channels+channel];v10=raw[(y0*w+x1)*channels+channel]
                v01=raw[(y1*w+x0)*channels+channel];v11=raw[(y1*w+x1)*channels+channel]
                return (v00*(1-fx)+v10*fx)*(1-fy)+(v01*(1-fx)+v11*fx)*fy
            if a.conversion=='cpu':
                result.extend(clipped(bilinear(c,4)) for c in range(3));continue
            lum=bilinear()/255;cb=raw[w*h+cy*(w//2)+cx]/255;cr=raw[w*h+w*h//4+cy*(w//2)+cx]/255
            if frame['fullRange']:
                cb-=.5;cr-=.5;r=lum+1.5748*cr;g=lum-.187324*cb-.468124*cr;b=lum+1.8556*cb
            else:
                lum=(lum-16/255)*255/219;cb-=128/255;cr-=128/255;r=lum+1.792741*cr;g=lum-.213249*cb-.532909*cr;b=lum+2.112402*cb
            result.extend(clipped(v*255) for v in (r,g,b))
    return result

maximum=0; means=[]
with tempfile.TemporaryDirectory(prefix='fw-foveation-') as td:
    td=pathlib.Path(td)
    for number,frame in enumerate(frames):
        chain=frame.get('fallbackChain',False)
        assert not chain or not a.no_resize, 'fallback chain must exercise its real transitions'
        dumpSequence=100 if chain else 10-firstPicture
        duration=6.0 if chain else 1.2
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(10)
        (td/'url').write_text(f'http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test')
        errors=[];acks=[]; received=[]
        def server():
            try:
                c,_=listener.accept();c.settimeout(5)
                request=b''
                while b'\r\n\r\n' not in request: request+=c.recv(1)
                assert b'&foveation=1' in request
                key=next(v.split(b':',1)[1].strip() for v in request.split(b'\r\n') if v.lower().startswith(b'sec-websocket-key:'))
                accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                c.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                hello={'t':'hello','r':'reconnect','foveation':1,'gazeSession':'test-session'}
                if frame.get('mode')==2:
                    assert b'&atlas=1' in request
                    hello['atlas']=1
                send(c,hello)
                start=time.monotonic();next_frame=start; seq=0
                while time.monotonic()-start<duration:
                    if select.select([c],[],[],.005)[0]:
                        op,raw=receive(c)
                        if op!=1:continue
                        m=json.loads(raw)
                        if m['t']=='ping':send(c,{'t':'pong','c':m['c'],'a':m['c']})
                        if m['t']=='rx':received.append(m['s'])
                        if m['t']=='fd':acks.extend(v[0] for v in m['f'])
                    if time.monotonic()>=next_frame and seq+firstPicture<len(sequences[frame['name']]):
                        # Generator uses one Encoder: size changes at4, then
                        # gaze/layout changes at7 within the same reference chain.
                        packet=bytearray(sequences[frame['name']][seq + firstPicture])
                        seq+=1;packet[9:13]=struct.pack('!I',seq)
                        send(c,packet,2);next_frame+=1/30
                send(c,{'t':'close'})
                try:
                    while True:receive(c)
                except (EOFError,ConnectionError,socket.timeout):pass
                c.close()
            except Exception as e: errors.append(repr(e))
            finally:listener.close()
        thread=threading.Thread(target=server);thread.start()
        dump=td/'out.ppm';dump.unlink(missing_ok=True)
        cmd=[str(a.binary.resolve()),'--no-vr','--foveation','--url-file',str(td/'url'),'--seconds',str(duration+1),'--decoder',a.decoder,'--conversion',a.conversion,
             '--dump',str(dump),'--dump-seq',str(dumpSequence),'--state-file',str(td/'state')]
        if not a.use_display:cmd=['xvfb-run','-a',*cmd]
        # Isolated QCOM processes need teardown time; this does not delay any
        # in-process stream transition or change submitted picture cadence.
        if a.decoder=='v4l2': time.sleep(3)
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=12);thread.join(7)
        assert result.returncode==0 and not errors,(frame['name'],result.stderr,errors)
        state=json.loads((td/'state').read_text())
        assert state['errors']==0 and state['decoderTimingHighWater']<=64,state
        if chain:
            assert state['decoderFormatReopens']==2 and not state['foveationActive'],state
            assert any(1<=s<=60 for s in acks) and any(61<=s<=120 for s in acks) and any(s>120 for s in acks),acks
        assert dumpSequence in received and acks, (received,acks)
        with dump.open('rb') as f:
            assert f.readline()==b'P6\n';w,h=map(int,f.readline().split());assert f.readline()==b'255\n';pixels=f.read()
        assert (w,h)==(frame['width'],frame['height']),(w,h,frame)
        expected=oracle(frame)
        assert len(pixels)==len(expected)
        errorsByChannel=[abs(x-y) for x,y in zip(pixels,expected)]
        maxError=max(errorsByChannel);meanError=sum(errorsByChannel)/len(errorsByChannel)
        maximum=max(maximum,maxError);means.append(meanError)
        if a.preview and number==0:a.preview.write_bytes(dump.read_bytes())
        # Texture filtering and shader arithmetic round within 1 byte. CPU swscale
        # conversions are compared with FFmpeg's same decoder/converter path.
        assert maxError<=2,(frame['name'],maxError,meanError)
        print(json.dumps({'fixture':frame['name'],'maxChannelError':maxError,'meanChannelError':meanError}),flush=True)
print(json.dumps({'result':'PASS','conversion':a.conversion,'decoder':a.decoder,'fixtures':len(frames),'resize':not a.no_resize,'fallbackChain':any(f.get('fallbackChain',False) for f in frames),'maxChannelError':maximum,'meanChannelError':sum(means)/len(means),
                  'coverage':'Metal NV12 -> VT H264 -> WS -> PTS mapping -> native decode -> one GL texture; gaze corners, continuous motion, both ranges; source resize when resize=true'}))
