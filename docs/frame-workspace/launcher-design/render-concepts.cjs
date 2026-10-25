const {app,BrowserWindow}=require('electron'); const fs=require('fs'),path=require('path');
app.dock?.hide();app.on('window-all-closed',()=>{});
app.whenReady().then(async()=>{try{
for(const [name,w,h] of [['concepts',1500,900],['icon-a',1024,1024],['icon-b',1024,1024],['icon-c',1024,1024]]){
 const win=new BrowserWindow({width:w,height:h,useContentSize:true,show:false,transparent:true,frame:false,webPreferences:{offscreen:true}});
 await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<body style="margin:0;background:transparent">'+fs.readFileSync(path.join(__dirname,name+'.svg'),'utf8')+'</body>'));
 await new Promise(r=>setTimeout(r,180));fs.writeFileSync(path.join(__dirname,name+'.png'),(await win.webContents.capturePage()).resize({width:w,height:h}).toPNG());win.destroy();}
process.stdout.write('Rendered original icon concepts\n');}catch(e){process.stderr.write(String(e)+'\n');process.exitCode=1;}app.quit();});
