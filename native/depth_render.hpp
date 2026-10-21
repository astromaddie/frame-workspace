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
      auto fs=compile(GL_FRAGMENT_SHADER,std::string("#version 120\nuniform vec3 uTip;uniform float uPx;uniform vec2 size,halfWindow;uniform vec2 points[10];uniform int count;uniform float bevelRadius;uniform mat4 local;\n")+ChromeRenderer::materialSource()+R"GLSL(
void main(){
 vec2 p=(gl_TexCoord[0].xy-.5)*size; p.y=-p.y;
 float dist=1e3; bool inside=false; vec2 grad=vec2(0.);
 for(int i=0;i<10;i++) {if(i>=count)break;int j=i+1;if(j==count)j=0;vec2 a=points[i],b=points[j];
  vec2 delta=p-closest(p,a,b);float d=length(delta);if(d<dist){dist=d;grad=away(delta,vec2(0.,1.));}
  if((a.y>p.y)!=(b.y>p.y) && p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x)inside=!inside;
 }
 float sd=inside?-dist:dist;vec2 roundedGradient;
 float rounded=sdBox(p,size*.5,bevelRadius,roundedGradient);
 if(rounded>sd){grad=roundedGradient;} // Rounded lighting only; the shell stays closed.
 float aa=clamp(.5-sd/uPx,0.,1.);
 vec3 pos=(local*vec4(p,0.,1.)).xyz;
 vec3 normal=normalize((local*vec4(inside? -grad*.45*exp(-dist/.002):vec2(0.),1.,0.)).xyz);
 vec3 c=lit(metal(dot(pos.xy/halfWindow,SHEEN)*.5+.5,false),normal);
 c=tipLight(c,pos.xy,normal);
 if(dist<uPx*1.2)c=mix(c,BORDER,.35);
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
    glUniform1f(u("bevelRadius"),std::min(wall.bevelRadius,std::min(wall.width,wall.height)*.45f));
    glUniform2f(u("size"),wall.width,wall.height);glUniform2f(u("halfWindow"),layout.width/2+layout.bezel(),layout.height/2+layout.bezel());glUniform1f(u("uPx"),std::max(wall.width/w,wall.height/h));
    glUniform3f(u("uTip"),state.tipX,state.tipY,state.tip?1:0);glUniform1i(u("count"),int(wall.polygon.size()));glUniform2fv(u("points"),GLsizei(wall.polygon.size()),wall.polygon[0].data());
    const auto &m=wall.local;float matrix[16]={m.m[0][0],m.m[1][0],m.m[2][0],0,m.m[0][1],m.m[1][1],m.m[2][1],0,m.m[0][2],m.m[1][2],m.m[2][2],0,m.m[0][3],m.m[1][3],m.m[2][3],1};glUniformMatrix4fv(u("local"),1,false,matrix);
    glBegin(GL_QUADS);glTexCoord2f(0,0);glVertex2f(-1,-1);glTexCoord2f(1,0);glVertex2f(1,-1);glTexCoord2f(1,1);glVertex2f(1,1);glTexCoord2f(0,1);glVertex2f(-1,1);glEnd();glUseProgram(0);glBindFramebuffer(GL_FRAMEBUFFER,0);glViewport(viewport[0],viewport[1],viewport[2],viewport[3]);
  }
};
}
