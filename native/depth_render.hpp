#pragma once
#include "chrome.hpp"
namespace fw {
class FrameWallRenderer {
  GLuint program=0,framebuffer=0;
public:
  void release(){if(program)glDeleteProgram(program);if(framebuffer)glDeleteFramebuffers(1,&framebuffer);program=framebuffer=0;}
  ~FrameWallRenderer(){release();}
  void render(const FrameWall &wall,const HandleLayout &layout,const ChromeState &state,GLuint texture) {
    if(!program){
      auto compile=[](GLenum kind,const std::string &source){GLuint sh=glCreateShader(kind);const char *s=source.c_str();glShaderSource(sh,1,&s,nullptr);glCompileShader(sh);GLint ok;glGetShaderiv(sh,GL_COMPILE_STATUS,&ok);if(!ok)throw std::runtime_error("wall shader compilation failed");return sh;};
      auto vs=compile(GL_VERTEX_SHADER,"#version 120\nvoid main(){gl_Position=gl_Vertex;gl_TexCoord[0]=gl_MultiTexCoord0;}");
      auto fs=compile(GL_FRAGMENT_SHADER,std::string("#version 120\n#define MAX_POINTS ")+std::to_string(FrameWall::maxPoints)+"\nuniform vec3 uTip;uniform float uPx;uniform vec2 size,halfWindow;uniform vec2 points[MAX_POINTS];uniform int count;uniform float bevelRadius,depthDirection,joinOverlap;uniform mat4 local;\n"+ChromeRenderer::materialSource()+R"GLSL(
void main(){
 vec2 p=(gl_TexCoord[0].xy-.5)*size; p.y=-p.y;
 float dist=1e3; bool inside=false;
 for(int i=0;i<MAX_POINTS;i++) {if(i>=count)break;int j=i+1;if(j==count)j=0;vec2 a=points[i],b=points[j];
  vec2 delta=p-closest(p,a,b);float d=length(delta);dist=min(dist,d);
  if((a.y>p.y)!=(b.y>p.y) && p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)inside=!inside;
 }
 // Intersect this cross-section with the actual faceted polygon. Its bounding
 // box includes bend sagitta, so p.y/size.y would flatten curved wall normals.
 float lo=1e3,hi=-1e3;
 for(int i=0;i<MAX_POINTS;i++){if(i>=count)break;int j=i+1;if(j==count)j=0;
  vec2 a=points[i],b=points[j];
  if(abs(b.x-a.x)>1e-7 && p.x>=min(a.x,b.x)-joinOverlap && p.x<=max(a.x,b.x)+joinOverlap){
   float y=mix(a.y,b.y,clamp((p.x-a.x)/(b.x-a.x),0.,1.));lo=min(lo,y);hi=max(hi,y);
  }
 }
 float sd=inside?-dist:dist;
 float aa=clamp(.5-(sd-joinOverlap)/uPx,0.,1.);
 float t=clamp((p.y-lo)/max(hi-lo,1e-5),0.,1.);
 // An elliptical pillow turns continuously from rear to front, rather than
 // adding a tiny bevel to an otherwise flat slab. Both adjacent walls turn
 // toward the same corner bisector, hiding the flat-overlay junction.
 float angle=(t-.5)*2.65;
 float end=1.-smoothstep(0.,bevelRadius,max(size.x*.5-joinOverlap-abs(p.x),0.));
 vec3 n=normalize(vec3(sign(p.x)*end*cos(angle), sin(angle), cos(angle)));
 vec3 pos=(local*vec4(p,0.,1.)).xyz;
 vec3 normal=normalize((local*vec4(n,0.)).xyz);
 vec3 base=metal(dot(pos.xy/halfWindow,SHEEN)*.5+.5,false);
 vec3 c=lit(base,normal);
 // Soft contact shading under the front lip; no dark stroked polygon seams.
 float front=depthDirection>0.?t:1.-t;
 // A broad reflected fill keeps the rolled rear metal silver-lavender.
 // It is strongest on the back-facing part of the pillow, away from the key light.
 c+=base*.18*(1.-front);
 c*=1.-.10*smoothstep(.86,1.,front);
 c=tipLight(c,pos.xy,normal);
 // Feather the rear corner over 1.2 mm. Closed support planes and their
 // overlap remain intact; a large rounded alpha cutout would expose a hole.
 float cornerSoft=end*(1.-smoothstep(0.,.2,front));
 aa=clamp(.5-(sd-joinOverlap)/mix(uPx,max(uPx,.0012),cornerSoft),0.,1.);
 gl_FragColor=vec4(c*aa,aa);
})GLSL");
      program=glCreateProgram();glAttachShader(program,vs);glAttachShader(program,fs);glLinkProgram(program);glDeleteShader(vs);glDeleteShader(fs);
      GLint ok;glGetProgramiv(program,GL_LINK_STATUS,&ok);if(!ok)throw std::runtime_error("wall shader link failed");glGenFramebuffers(1,&framebuffer);
    }
    int w,h;ChromeRenderer::textureSize(wall.width,wall.height,false,w,h);
    // Thin edge silhouettes still need several samples across their thickness.
    h=std::max(h,32);
    GLint viewport[4];glGetIntegerv(GL_VIEWPORT,viewport);
    glBindTexture(GL_TEXTURE_2D,texture);GLint tw,th;glGetTexLevelParameteriv(GL_TEXTURE_2D,0,GL_TEXTURE_WIDTH,&tw);glGetTexLevelParameteriv(GL_TEXTURE_2D,0,GL_TEXTURE_HEIGHT,&th);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    if(tw!=w||th!=h)glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,w,h,0,GL_RGBA,GL_UNSIGNED_BYTE,nullptr);
    glBindFramebuffer(GL_FRAMEBUFFER,framebuffer);glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,texture,0);
    if(glCheckFramebufferStatus(GL_FRAMEBUFFER)!=GL_FRAMEBUFFER_COMPLETE)throw std::runtime_error("wall framebuffer incomplete");
    glViewport(0,0,w,h);glDisable(GL_BLEND);glDisable(GL_DEPTH_TEST);glUseProgram(program);
    auto u=[&](const char *n){return glGetUniformLocation(program,n);};
    glUniform1f(u("bevelRadius"),wall.bevelRadius);
    glUniform1f(u("depthDirection"),wall.depthDirection);glUniform1f(u("joinOverlap"),FrameWall::joinOverlap);
    glUniform2f(u("size"),wall.width,wall.height);glUniform2f(u("halfWindow"),layout.width/2+layout.bezel(),layout.height/2+layout.bezel());glUniform1f(u("uPx"),std::max(wall.width/w,wall.height/h));
    glUniform3f(u("uTip"),state.tipX,state.tipY,state.tip?1:0);glUniform1i(u("count"),int(wall.polygon.size()));glUniform2fv(u("points"),GLsizei(wall.polygon.size()),wall.polygon[0].data());
    const auto &m=wall.local;float matrix[16]={m.m[0][0],m.m[1][0],m.m[2][0],0,m.m[0][1],m.m[1][1],m.m[2][1],0,m.m[0][2],m.m[1][2],m.m[2][2],0,m.m[0][3],m.m[1][3],m.m[2][3],1};glUniformMatrix4fv(u("local"),1,false,matrix);
    glBegin(GL_QUADS);glTexCoord2f(0,0);glVertex2f(-1,-1);glTexCoord2f(1,0);glVertex2f(1,-1);glTexCoord2f(1,1);glVertex2f(1,1);glTexCoord2f(0,1);glVertex2f(-1,1);glEnd();glUseProgram(0);glBindFramebuffer(GL_FRAMEBUFFER,0);glViewport(viewport[0],viewport[1],viewport[2],viewport[3]);
  }
};
}
