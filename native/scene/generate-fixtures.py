#!/usr/bin/env python3
"""Reproducible synthetic scene textures. Uses the host's Cairo dependency."""
import ctypes as C
import ctypes.util
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
lib = C.CDLL(ctypes.util.find_library('cairo') or '/opt/homebrew/lib/libcairo.2.dylib')

def api(name, result, *args):
    fn = getattr(lib, 'cairo_' + name)
    fn.restype, fn.argtypes = result, list(args)
    return fn

P, D, I = C.c_void_p, C.c_double, C.c_int
surface = api('image_surface_create', P, I, I, I)
create = api('create', P, P)
rgba = api('set_source_rgba', None, P, D, D, D, D)
rect = api('rectangle', None, P, D, D, D, D)
fill = api('fill', None, P)
move = api('move_to', None, P, D, D)
font = api('set_font_size', None, P, D)
text = api('show_text', None, P, C.c_char_p)
write = api('surface_write_to_png', I, P, C.c_char_p)
destroy = api('destroy', None, P)
free = api('surface_destroy', None, P)

def image(name, w, h, draw):
    s = surface(0, w, h)
    cr = create(s)
    def box(x,y,width,height,color):
        rgba(cr,*color); rect(cr,x,y,width,height); fill(cr)
    def label(x,y,value,size=28,color=(.16,.20,.27,1)):
        rgba(cr,*color); font(cr,size); move(cr,x,y); text(cr,value.encode())
    draw(box,label)
    assert write(s,str(ROOT/name).encode()) == 0
    destroy(cr); free(s)

def document(box, label):
    box(0,0,1440,900,(.95,.96,.98,1)); box(0,0,1440,66,(.87,.89,.93,1))
    label(30,44,'Field Notes — Project Atlas',30)
    box(0,66,250,834,(.90,.92,.95,1))
    for i,t in enumerate(['Overview','Design Notes','Milestones','Reference']): label(28,126+i*56,t,24)
    label(300,145,'A Workspace In Three Dimensions',42)
    label(300,204,'Project Brief / October 21',24,(.40,.46,.55,1))
    for i,t in enumerate(['Arrange Ideas Where They Belong','Keep Reference Material Within Reach','Bring The Current Task Into Focus']):
        label(310,292+i*82,'0'+str(i+1),22,(.30,.49,.75,1)); label(370,292+i*82,t,28)
    box(300,530,1080,240,(.86,.91,.97,1)); label(332,582,'Next Milestone',30)
    label(332,636,'Review The Working Prototype',27); label(332,685,'Thursday / 10:30',25)
    label(300,840,'Draft 04     •     Saved Locally',22,(.43,.48,.57,1))

def code(box,label):
    box(0,0,1440,1000,(.09,.12,.17,1)); box(0,0,1440,65,(.14,.18,.24,1))
    label(30,43,'Workbench — Scene.cpp',29,(.84,.88,.95,1))
    for i,t in enumerate(['// Keep Every Surface In Its Own Frame','struct Workspace {','    Panel reference;','    Panel document;','    Shelf applications;','};','','void update(float dt) {','    focus.advance(dt);','    panels.render();','    shelf.draw();','}']):
        label(38,128+i*52,f'{i+1:02}',22,(.40,.46,.56,1)); label(112,128+i*52,t,29,(.65,.80,.91,1))
    label(38,935,'Build Complete     •     0 Warnings',26,(.52,.82,.65,1))

def chart(box,label):
    box(0,0,1440,1000,(.97,.97,.96,1)); box(0,0,1440,65,(.88,.91,.91,1))
    label(30,44,'Metrics — Weekly Summary',29); label(70,153,'Progress By Milestone',42)
    for i,(name,value) in enumerate([('Explore',.45),('Build',.72),('Review',.58),('Deliver',.88)]):
        y=255+i*145; label(75,y+42,name,29); box(280,y,1030,70,(.88,.91,.92,1)); box(280,y,1030*value,70,(.25,.55,.59,1)); label(300+1030*value,y+46,f'{int(value*100)}',29)
    label(75,925,'Illustrative Data / Updated Today',25,(.40,.48,.50,1))

def main():
    image('document.png',1440,900,document)
    image('code.png',1440,1000,code)
    image('metrics.png',1440,1000,chart)
    for i,(name,letter,color) in enumerate([('notes','N',(.29,.48,.78,1)),('workbench','W',(.19,.24,.34,1)),('metrics','M',(.25,.58,.59,1)),('files','F',(.43,.64,.85,1))]):
        image(name+'-icon.png',128,128,lambda b,t,l=letter,c=color:(b(10,10,108,108,c),t(37,91,l,73,(1,1,1,1))))
    image('menubar.png',1200,60,lambda b,t:t(22,41,'Notes     File     Edit     View     Format     Window     Help',27))
    image('status.png',372,60,lambda b,t:t(18,40,'Wi-Fi     84%     Wed 10:21',28))
    image('dropdown.png',440,380,lambda b,t:[t(28,52+i*66,v,28) for i,v in enumerate(['New Note','Open…','Recent Notes','Save','Export…'])])

    def placement(x,z,yaw,width,bend):
        c,s=math.cos(math.radians(yaw)),math.sin(math.radians(yaw))
        return dict(version=1,width=width,distance=math.hypot(x,z),curvature=0,bend=bend,matrix=[c,0,s,x,0,1,0,0,-s,0,c,z])

    def auxiliary(texture,rect,anchor='none',anchor_rect=None,**extra):
        return dict(texture=texture,surface=dict(t='surface',v=1,seq=1,visible=True,rect=rect,anchor=anchor,anchorRect=anchor_rect or rect,**extra))
    scene=dict(panels=[
        dict(id='notes',role='window',placement=placement(0,-1.65,0,1.30,dict(vertical=False,a=.10,b=.10)),texture='document.png',focused=True,focusSnap=.65,pillLaser=True),
        dict(id='workbench',role='window',placement=placement(-1.10,-1.85,28,1.10,dict(vertical=True,a=.06,b=.14)),texture='code.png'),
        dict(id='metrics',role='window',placement=placement(1.10,-1.85,-28,1.10,dict(vertical=False,a=.04,b=.18)),texture='metrics.png',focusGlow=.20)],
        apps=[dict(id=name,name=name.title(),icon=name+'-icon.png') for name in ['notes','workbench','metrics','files']],
        menubar=auxiliary('menubar.png',[0,0,600,30],hot=True,menusWidth=600),
        status=auxiliary('status.png',[0,0,186,30]),
        dropdown=auxiliary('dropdown.png',[59,30,220,190],'menubar',[0,0,600,30]))
    scene['menubar']['surface']['selectedRect']=[59,0,34,30]
    (ROOT/'example.json').write_text(json.dumps(scene,indent=2)+'\n')
    # A slim strip is the resting state: production MenuStripReveal expands while
    # a dropdown is open, so the resting evidence correctly hides the dropdown.
    scene['menubar']['surface'].pop('selectedRect', None)
    scene['menubar']['surface']['hot']=False
    scene['dropdown']['surface']['visible']=False
    (ROOT/'example-slim.json').write_text(json.dumps(scene,indent=2)+'\n')


if __name__ == "__main__":
    main()
