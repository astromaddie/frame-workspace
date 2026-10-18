// Optional visual verification: PLAYWRIGHT_MODULE=/path/to/playwright-core node tests/page/workspace_placement_browser.mjs
import {createRequire} from 'node:module';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
(async()=>{const root=fileURLToPath(new URL('../..', import.meta.url));const src=fs.readFileSync(root+'/ui/index.html','utf8');const grab=n=>src.match(new RegExp(`(?:async )?function ${n}\\([\\s\\S]*?\\n}\\n`))[0];
const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});const page=await browser.newPage();
const markup=src.slice(src.indexOf('<section class="panel" id="macview"'),src.indexOf('<section class="panel" id="privacy"')).replace('id="macview" hidden','id="macview"');
const state={screen:true,workspace:{state:'running',preset:'2880x2560',scale:1,quality:'sharp',captureSrc:'display:8',placement:{width:2.4,distance:2},display:{logicalWidth:2880,logicalHeight:2560,bufferWidth:2880,bufferHeight:2560},stream:{width:4096,height:3640,fps:30}},streams:[{src:'display:8'}]};
await page.setContent('<html><head>'+src.match(/<style>[\s\S]*?<\/style>/)[0]+'</head><body style="padding:16px"><main style="max-width:900px;margin:auto">'+markup+'</main></body></html>');
await page.addScriptTag({content:`const $=id=>document.getElementById(id);let mvSeq=0,mvState=${JSON.stringify(state)},mvWorkspaceBusy=false,mvPlacementBusy=false,mvPlacementError='',mvWorkspaceActivePreset=null,mvWorkspacePendingPreset=null;const esc=String,mvHealth=()=>'';async function loadMacView(){};let requests=[];async function api(path,body){requests.push(body);return {ready:true,placement:{width:body.width,distance:body.distance}}};${['workspacePlacement','renderWorkspacePlacement','adjustWorkspacePlacement','renderMvWorkspace'].map(grab).join('\n')};${src.slice(src.indexOf('$("macview").onclick ='),src.indexOf('\n// ---- pages:'))};$('mvQuality').value='sharp';renderMvWorkspace(mvState);`});
for(const width of [1000,390]) {await page.setViewportSize({width,height:900});await page.locator('#mvWidthPlus').click();await page.locator('#mvWidthMinus').click();if(await page.locator('#mvWidthValue').textContent()!=='2.4 m')throw Error('actual click failed');const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);if(overflow)throw Error('overflow '+width);await page.locator('#macview').screenshot({path:root+'/docs/frame-workspace/evidence/native-controls-inline-'+width+'.png'});}
console.log('PASS Chromium production markup/styles/handlers, actual +/- clicks, no horizontal overflow at 1000 and 390px');await browser.close();})();
