#!/usr/bin/env python3
"""Image-based checks of the actual --render-scene production path (no VR)."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(path, alpha=255, solid=None):
    rows = []
    for y in range(16):
        row = bytearray([0])
        for x in range(16):
            color = solid or [(255,0,0),(0,255,0),(0,0,255),(255,255,0)][(y//8)*2+x//8]
            row.extend((*color,alpha))
        rows.append(row)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',16,16,8,6,0,0,0))+
                     chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b''))


def read_png(path):
    data=path.read_bytes(); offset=8; compressed=b''
    while offset < len(data):
        n=struct.unpack('>I',data[offset:offset+4])[0]
        kind=data[offset+4:offset+8]; payload=data[offset+8:offset+8+n]; offset+=12+n
        if kind==b'IHDR':
            w,h,depth,color,*_=struct.unpack('>IIBBBBB',payload)
            assert depth==8 and color in (2,6)
            bpp=3 if color==2 else 4
        elif kind==b'IDAT': compressed+=payload
    raw=zlib.decompress(compressed); rows=[]; previous=bytearray(w*bpp); offset=0
    for _ in range(h):
        method=raw[offset]; offset+=1; row=bytearray(raw[offset:offset+w*bpp]); offset+=w*bpp
        for i in range(len(row)):
            a=row[i-bpp] if i>=bpp else 0; b=previous[i]; c=previous[i-bpp] if i>=bpp else 0
            if method==1: pred=a
            elif method==2: pred=b
            elif method==3: pred=(a+b)//2
            elif method==4:
                p=a+b-c; distances=[abs(p-a),abs(p-b),abs(p-c)]; pred=[a,b,c][distances.index(min(distances))]
            else:
                assert method==0; pred=0
            row[i]=(row[i]+pred)&255
        rows.append(row); previous=row
    return w,h,lambda x,y:tuple(rows[y][x*bpp:x*bpp+3])


def main():
    binary=Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='scene-test-',dir=os.environ.get('FRAME_NATIVE_TEST_TMP')) as directory:
        root=Path(directory); png(root/'corners.png')
        panel=dict(id='test',role='window',texture='corners.png',height=.8,
                   placement=dict(version=1,width=1.2,distance=2,curvature=0,
                                  matrix=[1,0,0,0,0,1,0,0,0,0,1,-2]))
        scene=dict(panels=[panel]); manifest=root/'scene.json'; out=root/'image.png'
        def render(*extra, ok=True):
            manifest.write_text(json.dumps(scene))
            run=subprocess.run([str(binary),'--render-scene',str(manifest),str(out),'--size','320x240',*extra],capture_output=True,text=True)
            assert (run.returncode==0)==ok,run.stderr
            return out.read_bytes() if ok else None
        for vertical,a,b in [(False,0,0),(False,.12,.12),(False,.04,.18),(True,.12,.12),(True,.04,.18)]:
            panel['placement']['bend']=dict(vertical=vertical,a=a,b=b)
            render(); w,h,pixel=read_png(out); assert (w,h)==(320,240)
            expected=[(255,0,0),(0,255,0),(0,0,255),(255,255,0)]
            for (x,y),color in zip([(148,108),(172,108),(148,132),(172,132)],expected):
                actual=pixel(x,y)
                assert all(abs(c-d)<10 for c,d in zip(actual,color)),(vertical,a,b,x,y,actual,color)
        baseline=out.read_bytes()
        assert render('--eye','.1,0,0')!=baseline
        assert render('--look','5,-3')!=baseline
        assert render('--fov','80')!=baseline
        png(root/'near.png',solid=(0,255,255))
        nearer=json.loads(json.dumps(panel)); nearer['id']='near'; nearer['texture']='near.png'
        nearer['placement']['matrix'][11]=-1.6
        scene['panels'].append(nearer)
        panel['focused']=True; render(); _,_,pixel=read_png(out)
        assert pixel(148,108)[0]>245 and pixel(148,108)[1]<10, 'focused foreground band'
        panel['focused']=False; render(); _,_,pixel=read_png(out)
        assert pixel(148,108)[0]<10 and pixel(148,108)[1]>245, 'nearest overlay within band'
        scene['panels'].pop()
        png(root/'corners.png',128); render(); _,_,pixel=read_png(out)
        red=pixel(148,108)
        assert 128<=red[0]<170 and red[1]<40 and red[2]<40,('premultiplied alpha',red)
        render('--size','0x240',ok=False)
        render('--eye','nan,0,0',ok=False)
        render('--fov','180',ok=False)
        render('--unknown','1',ok=False)
        panel['height']=-1; render(ok=False)
        panel.pop('height'); panel.pop('placement'); panel['slot']=0
        png(root/'corners.png'); first=render()
        panel['slot']=8; assert render()!=first, 'scene uses extended wall slot geometry'
        for invalid in (-1, 1.5, True, 1024, None):
            panel['slot']=invalid; render(ok=False)
    print('Scene render passed: flat/even/split/vertical corner orientation, camera, foreground ordering, premultiplied alpha, invalid input')


if __name__=='__main__':
    main()
