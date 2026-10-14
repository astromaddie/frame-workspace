/* The Control Center uses the shared page's guarded api() and status poll.
 * Browsing never starts a stream, installs a helper or changes the headset. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FrameControlCenter = factory();
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const categories = [
    ['all', 'Everything', 'grid'], ['device', 'Device & audio', 'device'],
    ['vr', 'VR & comfort', 'eye'], ['apps', 'Apps & files', 'apps'],
    ['network', 'Connection & remote', 'network'], ['companion', 'Frame Control', 'settings'],
  ];
  // Each destination is an existing workflow, with its own guards and confirmations.
  const catalog = [
    {id:'audio', category:'device', icon:'audio', title:'Audio', kind:'control', label:'Available here',
      description:'Set the default headset output volume or mute it. Changes apply only when you choose Apply or Mute.',
      keywords:'sound speakers headphones loud quiet', special:'audio'},
    {id:'health', category:'device', icon:'pulse', title:'Battery & thermal health', kind:'read', label:'Read-only metrics',
      description:'Inspect charge, signed battery power, charger capability, battery temperature and health. Heat warnings use the kernel’s own hot and critical trip points.',
      keywords:'battery charging charger charge time estimate remaining power watts usb health heat temperature thermal sensor', special:'health', links:[['#comfort','Configure battery & heat alerts']]},
    {id:'storage', category:'device', icon:'files', title:'Storage & memory', kind:'read', label:'Read-only metrics',
      description:'Check free user and system storage plus available RAM. Manage installed apps in their existing libraries, or use SFTP for files.',
      keywords:'disk space root home free total ram memory capacity cleanup usage storage', special:'storage', links:[['#library','Installed games'],['#apps','Linux apps'],['#power','SFTP & remote tools']]},
    {id:'microphone', category:'device', icon:'audio', title:'Microphone & audio routing', kind:'headset', label:'In the headset',
      description:'Frame Control controls the default output volume only. Microphone mute, input gain, output selection, balance and per-app mixers have no verified API here.',
      keywords:'mic input microphone audio sink output device gain mute eq equalizer spatial audio balance headphones',
      guidance:'Check the headset’s audio controls or the app’s own audio settings for these adjustments.'},
    {id:'power', category:'device', icon:'power', title:'Power & sleep', kind:'flow', label:'Existing controls',
      description:'Sleep, restart or shut down with confirmation and your Developer Mode password. A sleeping Frame must be woken on the device.',
      keywords:'battery suspend reboot off awake idle timeout keep awake inhibitor', links:[['#power','Power controls']],
      guidance:'Set automatic sleep in the headset: Steam Settings → Power → Sleep after inactivity. Keep-awake is available through the existing script/MCP tools; there is no switch here.'},
    {id:'system', category:'device', icon:'device', title:'SteamOS & hardware', kind:'headset', label:'In the headset',
      description:'Check your current OS build above. Manage system updates, Developer Mode and hardware settings in Steam Settings on the Frame.',
      keywords:'firmware update beta stable channel developer password bluetooth controller battery pairing calibration brightness refresh rate display IPD lenses passthrough camera eye hand tracking foveation recovery factory reset',
      guidance:'Brightness, refresh rate, optics, controllers, Bluetooth and passthrough have no verified remote controls here. Check the headset’s own controls. Recovery and factory reset are manual procedures.'},
    {id:'performance', category:'vr', icon:'pulse', title:'Performance monitor', kind:'read', label:'Read-only metrics',
      description:'CPU, GPU clock, app and compositor timings come from the Frame. Unavailable timings stay empty, including when SteamVR is in standby.',
      keywords:'fps frame rate gpu cpu heat temperature thermal sensor render memory', special:'performance', links:[['#vrTools','HUD & performance tools']]},
    {id:'view', category:'vr', icon:'eye', title:'Headset view & casting', kind:'flow', label:'Existing controls',
      description:'Capture the lenses or desktop, stream live video, and control a focused panel. Casting can reveal private content.',
      keywords:'screenshot camera video capture live stream desktop mirror control eye', links:[['#view','Open headset view'],['#shots','Screenshots']]},
    {id:'comfort', category:'vr', icon:'clock', title:'Family & comfort', kind:'flow', label:'Existing controls',
      description:'Set a session limit, breaks and check-ins, with battery and heat alerts. Headset reminders continue if your companion disconnects.',
      keywords:'parent child timer minutes notifications health break low battery', links:[['#comfort','Configure a session']],
      guidance:'A reminder, not a parental lock. Save and pause before the session returns to Steam Home.'},
    {id:'playspace', category:'vr', icon:'space', title:'Playspace & movement', kind:'headset', label:'In the headset',
      description:'Use SteamVR’s recenter and room setup. Choose seated play, snap-turn, teleport and vignettes in each game’s own settings.',
      keywords:'boundary guardian tracking origin floor standing seated motion sickness locomotion turn recenter room',
      guidance:'No verified universal locomotion switch or remote tracking-origin control is available.'},
    {id:'panels', category:'vr', icon:'grid', title:'Panels & workspace', kind:'flow', label:'Existing controls',
      description:'Find an open panel and bring it forward, or open the panel switcher in the headset. Move, size and dock panels with SteamVR’s dashboard.',
      keywords:'overlay window layout float world curvature docking theatre multitasking', links:[['#panels','Open panel switcher']],
      guidance:'Saved spatial layouts are not available. Panel placement stays in SteamVR.'},
    {id:'steam', category:'apps', icon:'game', title:'Steam library', kind:'flow', label:'Existing controls',
      description:'Browse owned games and Frame ratings, install with progress, or launch an installed game. Purchases and license prompts use Steam.',
      keywords:'games download install store owned compatibility rating verified proton', links:[['#library','Installed games'],['#getgames','Library & store']]},
    {id:'android', category:'apps', icon:'apps', title:'Android apps', kind:'flow', label:'Existing controls',
      description:'Discover apps, manage Lepton instances, and launch, stop, test or remove installed apps. View compatibility reports before installing.',
      keywords:'apk fdroid lepton application sideload sources repository reports', links:[['#appStore','Discover apps'],['#androidLibrary','Manage installed apps']]},
    {id:'sources', category:'apps', icon:'shield', title:'App sources & compatibility', kind:'flow', label:'Existing controls',
      description:'Choose catalogue sources or add a signed F-Droid repository from Sources. Review install availability and compatibility reports for the exact app.',
      keywords:'fdroid f-droid repository source fingerprint trust signature compatibility report apk sidequest data backup restore', links:[['#appStore','Catalogue & sources'],['#androidLibrary','Apps & reports']],
      guidance:'Android data backup and restore are script-only. This interface does not provide an app-data recovery workflow.'},
    {id:'display', category:'apps', icon:'display', title:'Android display & text', kind:'flow', label:'Existing controls',
      description:'Choose a running Android instance, then set resolution, UI density and text size, or reset to its defaults.',
      keywords:'dpi scaling font scale sharp native 1920 1080 2560 1440 resolution screen text accessibility', links:[['#display','Configure app display']],
      guidance:'Applies to that Lepton instance, not the headset’s lenses. Persistence after relaunch is unverified.'},
    {id:'sideload', category:'apps', icon:'game', title:'Sideloaded games', kind:'flow', label:'Existing controls',
      description:'Send a game folder, ZIP or executable. Frame Control selects Proton or the Linux runtime and adds a Steam library entry.',
      keywords:'windows exe zip linux devkit runtime shortcuts launch remove', links:[['#sideloaded','Manage titles'],['#transfer','Send a game']]},
    {id:'files', category:'apps', icon:'files', title:'Files & clipboard', kind:'flow', label:'Existing controls',
      description:'Send files to ~/Downloads, or send text to the Frame’s clipboard. Clipboard transfer needs the desktop panel open.',
      keywords:'upload transfer downloads paste copy text sftp storage', links:[['#transfer','Send files & text']]},
    {id:'media', category:'apps', icon:'play', title:'Video & stereo photos', kind:'flow', label:'Preview feature',
      description:'Send and play H.264/H.265 video, SBS/OU photos or small splats with the built-in player. Choose a layout and theatre surround.',
      keywords:'movie photo sbs ou 3d splat gaussian theatre media player', links:[['#media','Open media player']],
      guidance:'Seeking, subtitles, native HEIC/MPO and large splat scenes are not supported yet.'},
    {id:'flatpak', category:'apps', icon:'apps', title:'Linux desktop apps', kind:'flow', label:'Existing controls',
      description:'Install user Flatpaks by Flathub ID, choose quick picks like VLC and Moonlight, and manage installed apps.',
      keywords:'flatpak firefox vlc remmina moonlight flathub install remove', links:[['#apps','Manage Linux apps']]},
    {id:'connection', category:'network', icon:'network', title:'Headsets & addresses', kind:'flow', label:'Existing controls',
      description:'Manage headsets, LAN, USB-C, mDNS and Tailscale addresses. Inspect connection attempts and choose the device you want to control.',
      keywords:'wifi wi-fi network ip ssh pairing keys discovery alias tailscale usb offline reconnect', links:[['#devices','Manage headsets','desktop'],['#connection','Connection details']],
      guidance:'Join Wi-Fi networks on the headset. Frame Control manages connection addresses; it does not change Wi-Fi credentials.'},
    {id:'remote', category:'network', icon:'display', title:'Remote access', kind:'flow', label:'Existing controls',
      description:'Open SSH, SFTP, Steam Link or remote desktop using the host’s existing tools. Developer Mode enables headset SSH and xrdp.',
      keywords:'terminal shell remote desktop rdp xrdp streaming steam link sftp', links:[['#power','Open remote tools']]},
    {id:'input', category:'network', icon:'keyboard', title:'Keyboard & trackpad', kind:'flow', label:'Existing controls',
      description:'Type and point in the Frame’s apps with KDE Connect, or use Control on the live panel view. First use sets up the input helper.',
      keywords:'mouse pointer keyboard trackpad touch click kde input', links:[['#pad','Open keyboard & trackpad'],['#view','Panel control']]},
    {id:'mac', category:'network', icon:'display', title:'Mac in the headset', kind:'flow', label:'macOS only', platform:'mac',
      description:'Stream Mac screens or individual windows into headset panels. Choose quality and give each window its own display.',
      keywords:'macos mac window screen sharing stream quality sharp balanced light permissions', links:[['#macview','Configure Mac streaming']],
      guidance:'Requires Frame Control running on a Mac. It is unavailable when the server runs on Windows, Linux or the headset.'},
    {id:'privacy', category:'companion', icon:'shield', title:'Privacy & sharing', kind:'flow', label:'Companion settings',
      description:'Choose anonymous usage statistics, compatibility sharing and error details separately. Inspect what was sent and manage your optional contact email.',
      keywords:'telemetry analytics diagnostics consent data tracking email contact opt out', links:[['#privacy','Privacy settings']]},
    {id:'artwork', category:'companion', icon:'image', title:'Library artwork', kind:'flow', label:'Companion settings',
      description:'Configure an optional SteamGridDB key or refresh artwork. Without a key, source images and generated art remain available.',
      keywords:'cover image steamgriddb api key library art refresh', links:[['#artworkSettings','Artwork settings']]},
    {id:'assistant', category:'companion', icon:'spark', title:'Assistant & agents', kind:'flow', label:'Opt-in tools',
      description:'Use your own model endpoint and review proposed actions. Nothing goes to a model until you opt in; protected actions need approval.',
      keywords:'ai mcp model endpoint agent approvals chat assistant', links:[['/assistant','Open assistant']]},
    {id:'support', category:'companion', icon:'help', title:'Updates & diagnostics', kind:'flow', label:'Companion settings',
      description:'Check Frame Control updates in the desktop app or prepare a private problem report. Review the diagnostic preview before sending.',
      keywords:'app version bug feedback support update log error report licenses', links:[['#privacy','Updates & problem reports']]},
  ];
  const paths = {
    grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    device:'M3 7h18v10H3z M8 11v2 M16 11v2', eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    apps:'M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M17 14v6 M14 17h6',
    network:'M2 8a16 16 0 0 1 20 0 M5 12a11 11 0 0 1 14 0 M8 16a6 6 0 0 1 8 0 M12 20h.01',
    settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
    audio:'M3 9h4l5-4v14l-5-4H3z M16 8a6 6 0 0 1 0 8 M19 5a10 10 0 0 1 0 14',
    power:'M12 2v10 M6 5a9 9 0 1 0 12 0', pulse:'M2 12h5l3-8 4 16 3-8h5',
    clock:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 7v5l3 2',
    space:'M3 8l9-5 9 5v8l-9 5-9-5z M3 8l9 5 9-5 M12 13v8',
    game:'M6 8h12l3 9-3 2-4-4h-4l-4 4-3-2z M7 10v4 M5 12h4 M16 11h.01 M18 13h.01',
    display:'M3 4h18v13H3z M8 21h8 M12 17v4', files:'M3 5h7l2 3h9v12H3z',
    play:'M8 4l12 8-12 8z', keyboard:'M2 6h20v12H2z M6 10h.01 M10 10h.01 M14 10h.01 M18 10h.01 M6 14h12',
    shield:'M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6',
    image:'M3 3h18v18H3z M3 17l6-6 5 5 3-3 4 4 M16 7h.01',
    spark:'M12 2l3 7 7 3-7 3-3 7-3-7-7-3 7-3z',
    help:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M9 8a3 3 0 0 1 6 1c0 2-3 2-3 5 M12 17h.01',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.settings}"/></svg>`;
  function matches(item, query, category) {
    const text = [item.title, item.description, item.keywords, item.guidance, item.label,
      categories.find(([id])=>id === item.category)?.[1]].join(' ').toLowerCase();
    return (category === 'all' || item.category === category) && query.toLowerCase().trim().split(/\s+/).every(word => text.includes(word));
  }
  const number = (value, suffix = '', digits = 0) => Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits:digits}) + suffix : 'Unavailable';
  const gib = value => Number.isFinite(value) ? number(value / 1073741824, ' GiB', 1) : 'Unavailable';
  const duration = seconds => Number.isFinite(seconds) && seconds >= 0 ? seconds < 3600 ? `${Math.floor(seconds / 60)} min` : `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min` : 'Unavailable';
  function metrics(sample) {
    const s = sample || {}, b = s.battery || {}, p = s.performance || {}, h = s.disk?.home, m = s.memory;
    const capacity = b.percent;
    const idle = !!s.power && Number.isFinite(b.watts) && Math.abs(b.watts) < .5;
    const charging = b.status === 'Charging';
    const full = b.status === 'Full' || (!!s.power && capacity >= 100);
    const batteryState = !sample ? 'Connect to read charge and power' : !s.battery ? 'Battery sensor unavailable' :
      full ? 'Fully charged' : charging ? 'Charging' : idle ? 'Plugged in · not charging' : s.power ? 'Plugged in · on battery' : 'On battery';
    return {
      battery:number(capacity, '%'), batteryDetail:batteryState,
      storage:h ? gib(h.free) : 'Unavailable', storageDetail:h ? `${gib(h.total)} total · user storage` : 'User storage unavailable',
      memory:m && Number.isFinite(m.total) && Number.isFinite(m.available) ? gib(m.total - m.available) : 'Unavailable',
      memoryDetail:m ? `${gib(m.total)} total · system memory used` : 'Memory sensor unavailable',
      rootFree:gib(s.disk?.root?.free), rootTotal:gib(s.disk?.root?.total), memoryAvailable:gib(m?.available),
      fps:number(p.compositorFps, '', 1), fpsDetail:'Compositor FPS · not game FPS',
      power:number(b.watts, ' W', 1), charger:s.power ? `${s.power.type || 'Connected'} · ${number(s.power.watts, ' W', 1)}` : sample ? 'No charger reported' : 'Unavailable',
      batteryTemp:number(b.tempC, ' °C', 1), batteryHealth:b.health || 'Unavailable',
      estimate:charging ? (b.timeToFull > 0 ? `${duration(b.timeToFull)} to full` : 'Unavailable') : !full && !idle && b.timeToEmpty > 0 && b.timeToEmpty < 86400 ? `${duration(b.timeToEmpty)} remaining` : 'Unavailable',
      wifi:s.wifi?.ssid || 'Unavailable', signal:number(s.wifi?.signal, '%'), ip:s.ip || 'Unavailable',
      uptime:duration(s.uptime), os:s.os?.version || 'Unavailable', build:s.os?.build || 'Unavailable',
      temperature:number(s.temp, ' °C', 1), cpu:number(p.cpuPercent, '%', 1), gpu:number(p.gpuMHz, ' MHz', 1),
      appFps:number(p.appFps, '', 1), frame:number(p.frameMs, ' ms', 2), render:number(p.gpuMs, ' ms', 2), compositorCpu:number(p.compositorCpuMs, ' ms', 2),
      thermal:!Array.isArray(s.thermalAlerts) ? 'Trip data unavailable' : s.thermalAlerts.length ? s.thermalAlerts.map(a => `${a.zone || 'Sensor'} ${number(a.tempC, ' °C')} ≥ ${number(a.limitC, ' °C')}`).join('; ') : 'No hot / critical trip reported',
    };
  }
  // Strict validation and captured device identity apply even if a stale enabled
  // button is clicked during a disconnect, headset switch, or pending request.
  function createController({api, getContext}) {
    let busy = false;
    return {
      get busy() { return busy; },
      async audio(change) {
        if (busy) throw new Error('An audio change is still in progress.');
        const context = getContext();
        if (context.online !== true || !context.fresh || !context.sample?.volume) throw new Error('Connect and refresh the Frame before changing audio.');
        const keys = Object.keys(change);
        if (!keys.length || keys.some(k => !['level','muted'].includes(k)) ||
            ('level' in change && (typeof change.level !== 'number' || !Number.isFinite(change.level) || change.level < 0 || change.level > 1)) ||
            ('muted' in change && typeof change.muted !== 'boolean')) throw new Error('Use a volume from 0 to 100% and a valid mute state.');
        busy = true;
        try {
          const result = await api('/api/volume', change, context.device);
          if (getContext().generation !== context.generation) return null;
          return result;
        } finally { busy = false; }
      },
    };
  }
  function mount({document, api, refresh, refreshAudio = refresh, getContext, openConnection, platform}) {
    const get = id => document.getElementById(id), page = get('controlCenter');
    let category = 'all', sample = null, receivedAt = null, available = false, error = '', dirty = false, sampleGeneration = null, audioPending = false;
    const metric = (id, label) => `<div><dt>${label}</dt><dd data-cc-metric="${id}">Unavailable</dd></div>`;
    const perf = `<dl class="cc-readings">${[['appFps','Application FPS'],['frame','Compositor period'],['render','Render GPU time'],['compositorCpu','Compositor CPU'],['cpu','System CPU'],['gpu','GPU clock'],['temperature','Hottest sensor']].map(([id,label])=>metric(id,label)).join('')}</dl><p class="cc-note">GPU time is render time, not utilisation. The hottest sensor is not necessarily CPU temperature.</p><p class="cc-note" data-cc-metric="thermal">Trip data unavailable</p>`;
    const audio = `<div class="cc-audio"><label for="ccVolume">Output volume <output id="ccVolumeValue" for="ccVolume">—</output></label><input id="ccVolume" type="range" min="0" max="100" step="1" value="0" disabled><div class="cc-audio-actions"><button class="action small" id="ccApplyVolume" disabled>Apply volume</button><button class="small" id="ccMute" disabled>Mute</button><span id="ccAudioReadback" class="cc-note">Waiting for an audio reading</span></div><p class="cc-note" id="ccAudioResult" role="status"></p></div>`;
    const health = `<dl class="cc-readings">${[['battery','Charge'],['power','Battery power · signed'],['charger','Charger'],['estimate','Battery estimate'],['batteryTemp','Battery temperature'],['batteryHealth','Battery health']].map(([id,label])=>metric(id,label)).join('')}</dl>`;
    const storage = `<dl class="cc-readings">${[['storage','User storage free'],['rootFree','System storage free'],['rootTotal','System storage total'],['memoryAvailable','Memory available']].map(([id,label])=>metric(id,label)).join('')}</dl>`;
    get('ccCategories').innerHTML = categories.map(([id,label,symbol])=>`<button type="button" data-cc-category="${id}" aria-pressed="${id === 'all'}">${icon(symbol)}<span>${label}</span><span class="cc-category-count"></span></button>`).join('');
    const specials = {audio, performance:perf, health, storage};
    get('ccCards').innerHTML = catalog.map(item=>`<section class="cc-card" data-cc-card="${item.id}"><div class="cc-card-head"><span class="cc-icon">${icon(item.icon)}</span><span class="cc-kind cc-kind-${item.kind}">${item.label}</span></div><h2>${item.title}</h2><p>${item.description}</p>${specials[item.special] || ''}${item.guidance ? `<div class="cc-guidance">${item.guidance}</div>` : ''}${item.links ? `<div class="cc-links">${item.links.map(([href,label,desktop])=>`<a href="${href}"${desktop ? ' class="desk-only"' : ''}${item.platform === 'mac' ? ' data-cc-mac-link' : ''}>${label}<span aria-hidden="true">↗</span></a>`).join('')}</div>` : ''}</section>`).join('');
    // Host-only features stay searchable with a reason; only their unsupported
    // action disappears. The host API, rather than viewport/UA, decides availability.
    let macSupported = platform === 'darwin';
    const visibleCatalog = catalog;
    function filter() {
      const query = get('ccSearch').value;
      let count = 0;
      catalog.forEach(item => {
        const visible = visibleCatalog.includes(item) && matches(item, query, category);
        page.querySelector(`[data-cc-card="${item.id}"]`).hidden = !visible;
        if (visible) count++;
      });
      get('ccResultCount').textContent = `${count} ${count === 1 ? 'section' : 'sections'}`;
      get('ccEmpty').hidden = count > 0;
      get('ccClear').hidden = !query;
      page.querySelectorAll('[data-cc-category]').forEach(button => {
        button.setAttribute('aria-pressed', String(button.dataset.ccCategory === category));
        button.querySelector('.cc-category-count').textContent = visibleCatalog.filter(item=>matches(item, query, button.dataset.ccCategory)).length;
      });
      page.querySelectorAll('[data-cc-mac-link]').forEach(link=>{link.hidden = !macSupported;});
    }
    get('ccSearch').addEventListener('input', filter);
    function resetFilter() { get('ccSearch').value = ''; category = 'all'; filter(); get('ccSearch').focus(); }
    get('ccClear').onclick = () => { get('ccSearch').value = ''; filter(); get('ccSearch').focus(); };
    get('ccResetFilter').onclick = resetFilter;
    get('ccCategories').onclick = event => {
      const button = event.target.closest('[data-cc-category]');
      if (!button) return;
      category = button.dataset.ccCategory; filter();
    };
    page.querySelector('[href="#connection"]').onclick = event => { event.preventDefault(); openConnection(); };
    // The headset clock can differ from this client's. Age uses the client
    // receipt time, retained by the shared poll when it buffers an early sample.
    const isFresh = () => available && Number.isFinite(receivedAt) && Date.now() >= receivedAt && Date.now() - receivedAt <= 90000;
    const context = () => ({...getContext(), sample, fresh:isFresh() && sampleGeneration === getContext().generation});
    const controller = createController({api, getContext:context});
    function audioAvailability() {
      const enabled = context().online === true && context().fresh && !!sample?.volume && !controller.busy && !audioPending;
      ['ccVolume','ccApplyVolume','ccMute'].forEach(id=>{get(id).disabled = !enabled;});
    }
    function renderSample() {
      const values = metrics(isFresh() ? sample : null);
      page.querySelectorAll('[data-cc-metric]').forEach(el=>{el.textContent = values[el.dataset.ccMetric];});
      get('ccDeviceName').textContent = isFresh() ? sample.hostname || 'Steam Frame' : 'Steam Frame';
      const sampledAt = Number.isFinite(sample?.time) && sample.time > 0 && sample.time * 1000 <= 8640000000000000 ? new Date(sample.time * 1000) : null;
      get('ccSampleState').textContent = isFresh() ? `${sampledAt ? 'Sampled ' + sampledAt.toLocaleTimeString() : 'Received ' + new Date(receivedAt).toLocaleTimeString()} · updates every 30 seconds` :
        error || (receivedAt !== null ? 'Sample expired · refresh to read current metrics' : 'Connect your Frame to read live metrics');
      get('ccSampleState').classList.toggle('cc-offline', !isFresh());
      get('ccStatusLabel').textContent = isFresh() ? 'Connected' : 'No live sample';
      get('ccStatusDot').classList.toggle('on', isFresh());
      get('ccStatusNote').textContent = isFresh() ? 'Read-only status over SSH. Browse freely; controls run only when you use them.' :
        'Settings guides and companion tools remain available. Wake the Frame on the device, then reconnect.';
      get('ccBuild').textContent = isFresh() ? `SteamOS ${values.os} · build ${values.build}` : 'System information unavailable';
      const services = isFresh() ? sample.services || {} : {};
      get('ccServices').innerHTML = [['steamvr','SteamVR'],['desktop','Desktop'],['lepton','Lepton · port 5555'],['rdp','RDP process']].map(([key,label])=>`<span><i class="dot ${services[key] === true ? 'on' : ''}"></i>${label}<b>${typeof services[key] === 'boolean' ? services[key] ? 'Running' : 'Not detected' : 'Unavailable'}</b></span>`).join('');
      if (sample?.volume && isFresh()) {
        const pct = Math.round(sample.volume.level * 100);
        if (!dirty && !controller.busy && !audioPending) get('ccVolume').value = Math.min(100,Math.max(0,pct));
        get('ccVolumeValue').textContent = `${get('ccVolume').value}%`;
        get('ccAudioReadback').textContent = `Reported ${pct}% · ${sample.volume.muted ? 'muted' : 'unmuted'}`;
        get('ccMute').textContent = sample.volume.muted ? 'Unmute' : 'Mute';
      } else {
        get('ccAudioReadback').textContent = isFresh() ? 'Audio reading unavailable' : 'Waiting for an audio reading';
        get('ccVolumeValue').textContent = '—';
      }
      audioAvailability();
    }
    async function changeAudio(change) {
      if (audioPending) return;
      const generation = getContext().generation;
      audioPending = true;
      get('ccAudioResult').textContent = 'Applying…';
      try {
        const pending = controller.audio(change);
        audioAvailability();
        const result = await pending;
        if (!result || generation !== getContext().generation) return;
        if ('level' in change) dirty = false;
        get('ccAudioResult').textContent = `${result.message || 'Audio updated'}. Refreshing the reading…`;
        await refreshAudio();
        if (generation !== getContext().generation) return;
        if (!isFresh() || !sample?.volume) {
          get('ccAudioResult').textContent = 'Audio command sent; current output could not be confirmed. Reconnect and refresh.';
        } else {
          const reported = sample.volume;
          const matched = (!('level' in change) || Math.round(reported.level * 100) === Math.round(change.level * 100)) &&
            (!('muted' in change) || reported.muted === change.muted);
          get('ccAudioResult').textContent = `${matched ? 'Confirmed' : 'Command sent · reported output differs'}: ${Math.round(reported.level * 100)}%, ${reported.muted ? 'muted' : 'unmuted'}.${matched ? '' : ' Refresh to check again.'}`;
        }
      } catch (e) {
        if (generation !== getContext().generation) return;
        get('ccAudioResult').textContent = `Couldn’t apply audio: ${e.message}`;
      } finally { audioPending = false; renderSample(); }
    }
    get('ccVolume').oninput = () => { dirty = true; get('ccVolumeValue').textContent = `${get('ccVolume').value}%`; };
    get('ccApplyVolume').onclick = () => changeAudio({level:Number(get('ccVolume').value) / 100});
    get('ccMute').onclick = () => changeAudio({muted:!sample?.volume?.muted});
    get('ccRefresh').onclick = async () => {
      get('ccRefresh').disabled = true;
      try { await refresh(); } finally { get('ccRefresh').disabled = false; }
    };
    filter(); renderSample();
    // Expire UI readings even when the shared poll is paused in a hidden tab.
    const timer = setInterval(()=>{if (receivedAt !== null && available && !isFresh()) { available = false; renderSample(); }}, 5000);
    return {
      host(value) { macSupported = !value.mobile && value.os === 'macOS'; filter(); },
      status(value, receipt = Date.now(), read = {valid: true, error: null}) {
        // A redraw is not a successful read: preserve receipt age and any local
        // invalidation. Shared validity also carries failures across late mount.
        const replay = sample === value;
        receivedAt = replay ? (Number.isFinite(receivedAt) && Number.isFinite(receipt) ? Math.min(receivedAt, receipt) : receivedAt) : receipt;
        sample = value;
        if (!read.valid) {
          available = false; error = `No live sample${read.error?.message ? ' · ' + read.error.message : ''}`;
        } else if (!replay) { available = true; error = ''; }
        sampleGeneration = getContext().generation; renderSample();
      },
      unavailable(reason) { if (reason?.stale) return; available = false; error = `No live sample${reason?.message ? ' · ' + reason.message : ''}`; renderSample(); },
      connection(ok) { if (!ok) { available = false; renderSample(); } else audioAvailability(); },
      reset() { sample = null; receivedAt = null; available = false; error = ''; dirty = false; get('ccVolume').value = 0; get('ccAudioResult').textContent = ''; renderSample(); },
      destroy() { clearInterval(timer); },
    };
  }
  return {catalog, categories, matches, metrics, createController, mount};
});
