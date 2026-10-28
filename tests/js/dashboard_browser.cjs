// Real browser test against tests/test_dashboard_page.py's synthetic, temporary server only.
// Standard Node libraries; uses an isolated Chromium profile and the debugging pipe, no npm packages.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const [url, chromePath, output] = process.argv.slice(2);
if (!url || !/^http:\/\/127\.0\.0\.1:\d+\/$/.test(url)) throw Error('A scratch loopback fixture URL is required');
fs.mkdirSync(output,{recursive:true});
const profile = fs.mkdtempSync(path.join(os.tmpdir(),'dashboard-ui-browser-'));
const chrome = spawn(chromePath,['--headless=new','--remote-debugging-pipe','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
const exited = new Promise(resolve=>chrome.once('close',resolve));
let session, buffer='', seq=0, closed=false;
const pending=new Map(), events=new Map(), errors=[], requests=[], checks=[], failures=[];
chrome.stderr.on('data',()=>{});
chrome.on('exit',()=>{closed=true;for(const p of pending.values())p.reject(Error('Browser exited'));pending.clear();});
chrome.stdio[3].on('error',e=>{for(const p of pending.values())p.reject(e);pending.clear();});
chrome.stdio[4].on('data',data=>{buffer+=data;let i;while((i=buffer.indexOf('\0'))>=0){const raw=buffer.slice(0,i);buffer=buffer.slice(i+1);if(!raw)continue;const m=JSON.parse(raw);if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}}else if(m.sessionId===session){if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text);if(m.method==='Network.requestWillBeSent')requests.push(m.params.request.url);const e=events.get(m.method);if(e){events.delete(m.method);e(m.params);}}}});
function cmd(method,params={},sid=session){return new Promise((resolve,reject)=>{if(closed)return reject(Error('Browser closed'));const id=++seq;pending.set(id,{resolve,reject});const m={id,method,params};if(sid)m.sessionId=sid;chrome.stdio[3].write(JSON.stringify(m)+'\0');setTimeout(()=>{if(pending.has(id)){pending.delete(id);reject(Error('Timed out: '+method));}},15000).unref();});}
async function evaluate(expression){const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function waitFor(expression){for(let i=0;i<100;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}throw Error('Condition timed out: '+expression);}
async function load(){const loaded=new Promise(resolve=>events.set('Page.loadEventFired',resolve));await cmd('Page.navigate',{url});await loaded;await waitFor('typeof data!=="undefined"&&data&&!busy');await settle();}
async function settle(){await evaluate('document.fonts.ready');await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');}
async function click(selector){await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);await settle();}
async function saved(){await waitFor('!saving');}
async function viewport(w,h,mobile=false){await cmd('Emulation.setDeviceMetricsOverride',{width:w,height:h,deviceScaleFactor:1,mobile});await settle();}
async function shot(name){await settle();const r=await cmd('Page.captureScreenshot',{format:'png',fromSurface:true});fs.writeFileSync(path.join(output,name),Buffer.from(r.data,'base64'));}
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)failures.push(name);}
const app='article[data-key="electrogui"]';
const card=id=>`article[data-key="${id}"]`;
async function filters(search='',status=''){await evaluate(`document.getElementById('app-search').value=${JSON.stringify(search)};document.getElementById('app-search').dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('status-filter').value=${JSON.stringify(status)};document.getElementById('status-filter').dispatchEvent(new Event('change',{bubbles:true}));`);await settle();}
(async()=>{try{
 const target=await cmd('Target.createTarget',{url:'about:blank'},null);session=(await cmd('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
 await cmd('Page.enable');await cmd('Runtime.enable');await cmd('Network.enable');await cmd('Page.bringToFront');await cmd('Emulation.setFocusEmulationEnabled',{enabled:true});
 await viewport(1440,1000);await load();
 check('Compact view does not show previews by default',await evaluate('document.querySelectorAll(".preview").length===0'));
 check('Tailnet and local-only sections are both drawn',await evaluate('[...document.querySelectorAll("#main h2")].map(h=>h.textContent).join()==="On the tailnet,Only on this Mac"'));
 check('An unregistered listener is a card marked Not registered',await evaluate(`!!document.querySelector(${JSON.stringify(card('port-5173')+' .badge')})&&document.querySelector(${JSON.stringify(card('port-5173')+' .badge')}).textContent==="Not registered"`));
 check('The saved hidden app is counted',await evaluate('document.getElementById("showall").textContent==="Show all apps (1)"&&!document.querySelector(\'article[data-key="docs-site"]\')'));
 check('The saved order is applied',await evaluate('[...document.querySelectorAll("#main .grid")[0].children].map(n=>n.dataset.key).slice(0,2).join()==="notes,electrogui"'));
 check('Revision warning is visible without Diagnostics',await evaluate(`document.querySelector(${JSON.stringify(app+' .collapsed-notes')}).textContent.includes("restart to pick it up")`));
 check('An idle on-demand app says so',await evaluate(`document.querySelector(${JSON.stringify(card('photo-archive')+' .collapsed-notes')}).textContent.includes("On demand · not running")`));
 check('A down app shows its problem collapsed',await evaluate(`document.querySelector(${JSON.stringify(card('scratch-api'))}).classList.contains("down")&&document.querySelector(${JSON.stringify(card('scratch-api')+' .collapsed-notes')}).textContent.includes("connection refused")`));
 check('Desktop has no horizontal overflow',await evaluate('document.documentElement.scrollWidth<=innerWidth'));
 await shot('implemented-desktop.png');
 const beforeOpen=await evaluate('JSON.stringify(layout)');
 const point=await evaluate(`let r=document.querySelector(${JSON.stringify(app+' .open-app')}).getBoundingClientRect();({x:r.x+r.width/2,y:r.y+r.height/2})`);
 await cmd('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
 await new Promise(r=>setTimeout(r,150));
 const targets=(await cmd('Target.getTargets',{},null)).targetInfos;
 const opened=targets.find(t=>t.targetId!==target.targetId&&t.url===url+'healthz');
 check('Native Open launches the exact scratch destination in a new tab',!!opened);
 if(opened)await cmd('Target.closeTarget',{targetId:opened.targetId},null);
 check('Open does not write layout',beforeOpen===await evaluate('JSON.stringify(layout)'));
 check('Launch links retain target and noopener',await evaluate('[...document.querySelectorAll("a.open-app")].every(a=>a.target==="_blank"&&a.rel.includes("noopener"))'));
 check('This page is marked instead of linked',await evaluate(`!!document.querySelector(${JSON.stringify(card('apps-dashboard')+' .self-mark')})&&!document.querySelector(${JSON.stringify(card('apps-dashboard')+' .open-app')})`));
 await filters('5173');check('Search finds an app by its port',await evaluate('document.querySelectorAll("article").length===1&&document.querySelector("article").dataset.key==="port-5173"'));
 await filters('','down');check('Down filter shows only down apps',await evaluate('[...document.querySelectorAll("article")].map(a=>a.dataset.key).join()==="scratch-api"'));
 await filters('no-such-app');check('Empty filter state is explicit',await evaluate('document.querySelector(".empty").textContent.includes("No apps match")'));
 await filters();
 await click(app+' .diagnostics > summary');
 check('Diagnostics retain local and tailnet probe results and units',await evaluate(`let t=document.querySelector(${JSON.stringify(app+' .diagnostics')}).textContent;t.includes("200")&&t.includes("ms")&&t.includes("127.0.0.1:8778")&&t.includes("local.electrogui")`));
 check('Diagnostics link the codebase remote',await evaluate(`!!document.querySelector(${JSON.stringify(app+' .diagnostics a[href="https://github.com/chenruidong/electrogui"]')})`));
 await evaluate(`document.querySelector(${JSON.stringify(app+' .diagnostics > summary')}).focus();render()`);
 check('Background render preserves diagnostic expansion and keyboard focus',await evaluate(`document.querySelector(${JSON.stringify(app+' .diagnostics')}).open&&document.activeElement.dataset.control==="diagnostics"`));
 await click(app+' .diagnostics > summary');
 await evaluate(`document.querySelector(${JSON.stringify(app+' .organize > summary')}).focus()`);
 await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36});await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
 check('Keyboard Enter opens Organize',await evaluate(`document.querySelector(${JSON.stringify(app+' .organize')}).open`));
 await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 check('Escape closes Organize and returns focus',await evaluate(`!document.querySelector(${JSON.stringify(app+' .organize')}).open&&document.activeElement.dataset.control==="organize"`));
 await click(app+' .organize > summary');await click(app+' .view');
 check('Preview is explicitly selectable',await evaluate(`!!document.querySelector(${JSON.stringify(app+' .preview img')})`));await load();
 check('Preview choice persists on reload',await evaluate(`!!document.querySelector(${JSON.stringify(app+' .preview')})`));
 const orderBefore=await evaluate('layout.order.slice()');
 await click(app+' .organize > summary');await click(app+' .move[data-direction="-1"]');await saved();
 const orderAfter=await evaluate('layout.order.slice()');
 check('Move earlier persists an order change',JSON.stringify(orderBefore)!==JSON.stringify(orderAfter)&&orderAfter.indexOf('electrogui')<orderAfter.indexOf('notes'));
 check('Reorder keeps hidden and offline slots',orderBefore.every(s=>orderAfter.includes(s))&&orderAfter.includes('retired-app')&&orderAfter.includes('docs-site'));
 check('Reorder returns focus to its organization control',await evaluate('document.activeElement.dataset.control==="organize"'));
 await evaluate('window.__orderBeforeDrag=layout.order.slice()');
 const cardOrder=()=>evaluate(`[...document.querySelector(${JSON.stringify(app)}).parentElement.children].map(n=>n.dataset.key).join()`);
 // A mouse drag from `from` (inside the app's card) to the middle of another card in its section.
 async function drag(from){const a=await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(from)}).getBoundingClientRect();return {x:r.x+Math.min(12,r.width/2),y:r.y+r.height/2};})()`);
  const b=await evaluate(`(()=>{const c=[...document.querySelector(${JSON.stringify(app)}).parentElement.children],o=c[0].matches(${JSON.stringify(app)})?c[c.length-1]:c[0],r=o.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  await cmd('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',buttons:1,clickCount:1,...a});
  for(let i=1;i<=5;i++)await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',button:'left',buttons:1,x:a.x+(b.x-a.x)*i/5,y:a.y+(b.y-a.y)*i/5});
  await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',buttons:0,clickCount:1,...b});await settle();await saved();}
 let cardsBefore=await cardOrder(),orderBeforeDrag=await evaluate('JSON.stringify(layout.order)');await drag(app+' .launch-row .endpoint');
 check('Mouse drag from the card body reorders and saves',cardsBefore!==await cardOrder()&&orderBeforeDrag!==await evaluate('JSON.stringify(layout.order)'));
 const tabsBefore=(await cmd('Target.getTargets',{},null)).targetInfos.length;
 cardsBefore=await cardOrder();await drag(app+' .name a');
 check('Drag from the title link reorders without opening the app',cardsBefore!==await cardOrder()&&(await cmd('Target.getTargets',{},null)).targetInfos.length===tabsBefore);
 cardsBefore=await cardOrder();orderBeforeDrag=await evaluate('JSON.stringify(layout.order)');await drag(app+' .diagnostics > summary');
 check('A drag from Diagnostics does not reorder',cardsBefore===await cardOrder()&&orderBeforeDrag===await evaluate('JSON.stringify(layout.order)'));
 await evaluate('changeLayout({order:window.__orderBeforeDrag})');await saved();
 check('Hide is a top-right × beside Organize, not in its menu',await evaluate(`(()=>{const c=document.querySelector(${JSON.stringify(app)}),h=c.querySelector(".head > .card-tools > .hide"),cr=c.getBoundingClientRect(),hr=h&&h.getBoundingClientRect();return !!h&&h.textContent==="×"&&h.previousElementSibling.classList.contains("organize")&&!c.querySelector(".organize .hide")&&cr.right-hr.right<=16&&hr.top-cr.top<=16;})()`));
 await click(app+' .card-tools > .hide');
 check('× moves focus to Show all apps',await evaluate('document.activeElement.id==="showall"'));await saved();
 check('Hide saves the app id',await evaluate('layout.hidden.includes("electrogui")&&layout.hidden.includes("docs-site")'));
 await load();check('Hidden state survives reload',await evaluate(`!document.querySelector(${JSON.stringify(app)})&&document.getElementById("showall").textContent==="Show all apps (2)"`));
 await click('#showall');await saved();check('Show all restores every hidden app',await evaluate(`layout.hidden.length===0&&!!document.querySelector(${JSON.stringify(app)})&&!!document.querySelector(${JSON.stringify(card('docs-site'))})`));
 await evaluate('saveError="Could not save the order and hidden apps: synthetic error";render()');check('Save failure is visible',await evaluate('document.getElementById("errors").textContent.includes("synthetic error")'));
 await click(app+' .organize > summary');await click(app+' .move[data-direction="1"]');await saved();check('Successful retry clears prior save failure',await evaluate('!saveError&&!document.getElementById("errors").textContent.includes("synthetic error")'));
 await load();
 for(const w of [390,320,720]){await viewport(w,w===720?450:844,w<500);check(`${w}px has no horizontal overflow`,await evaluate('document.documentElement.scrollWidth<=innerWidth'));if(w===390)await shot('implemented-mobile.png');}
 await viewport(390,844,true);await click(app+' .diagnostics > summary');await evaluate(`document.querySelector(${JSON.stringify(app)}).scrollIntoView({block:"start"})`);await shot('implemented-mobile-diagnostics.png');
 check('Mobile Open, Organize, × and Diagnostics targets are at least 44px high',await evaluate('[...document.querySelectorAll(".open-app,.organize>summary,.head .hide,.diagnostics>summary")].every(n=>n.getBoundingClientRect().height>=44)'));
 await cmd('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'}]});check('Dark mode retains readable native theme tokens',await evaluate('getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()==="#121418"'));await shot('implemented-mobile-dark.png');
 await evaluate('localStorage.setItem("apps-dashboard.previews","{}");localStorage.setItem("apps-dashboard.scan","not json")');await load();
 check('Malformed saved preferences do not break rendering',await evaluate('document.querySelectorAll("article").length>0&&document.querySelectorAll(".preview").length===0'));
 check('No runtime errors',errors.length===0);
 const external=requests.filter(u=>/^https?:/.test(u)&&new URL(u).origin!==new URL(url).origin);
 check('No external application requests or private previews',external.length===0);
 const report={checks,failures,runtimeErrors:errors,externalRequests:external};fs.writeFileSync(path.join(output,'browser-checks.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }catch(e){try{await shot('failure.png');}catch{}console.error(e.stack);process.exitCode=1;}finally{if(!closed)try{await cmd('Browser.close',{},null);}catch{}chrome.kill();await Promise.race([exited,new Promise(r=>setTimeout(r,2500))]);fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:150});}if(failures.length)process.exitCode=1;
})();
