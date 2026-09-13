// Early PWA startup helpers.
// Runs before the main UI bundle so installed launches can paint with the
// correct native-like classes and capture browser install events early.
(function(){
  'use strict';
  var root=document.documentElement;

  // The server stamps one asset fingerprint token into shell URLs and this page,
  // but stale-client detection compares a semantic WebUI version. Keep those
  // identities separate: retain the raw asset token for cache/debug consumers
  // while exposing only the release version through the historical bundle
  // global consumed by checkWebUIVersionSkew().
  function semanticBundleVersion(value){
    if(value===null||value===undefined) return '';
    var raw=String(value);
    var decoded=raw;
    try{decoded=decodeURIComponent(raw);}catch(_){}
    return decoded.replace(/\+a[0-9a-f]{10}$/i,'');
  }
  try{
    var bundleVersion='';
    Object.defineProperty(window,'__HERMES_WEBUI_BUNDLE_VERSION__',{
      configurable:true,
      enumerable:true,
      get:function(){return bundleVersion;},
      set:function(value){
        var raw=value===null||value===undefined?'':String(value);
        window.__HERMES_WEBUI_ASSET_VERSION__=raw;
        bundleVersion=semanticBundleVersion(raw);
      }
    });
  }catch(_){}

  function mql(query){
    try{return window.matchMedia&&window.matchMedia(query).matches;}catch(_){return false;}
  }
  function isStandalone(){
    return window.navigator.standalone===true ||
      mql('(display-mode: standalone)') ||
      mql('(display-mode: fullscreen)') ||
      mql('(display-mode: window-controls-overlay)');
  }
  function isIOS(){
    return /iPad|iPhone|iPod/.test(window.navigator.userAgent||'') ||
      (window.navigator.platform==='MacIntel' && window.navigator.maxTouchPoints>1);
  }
  function syncMode(){
    var standalone=isStandalone();
    root.classList.toggle('pwa-standalone',standalone);
    root.classList.toggle('pwa-browser',!standalone);
    root.classList.toggle('pwa-ios',isIOS());
    root.classList.toggle('pwa-offline',window.navigator.onLine===false);
    root.dataset.pwaDisplayMode=standalone?'standalone':'browser';
    return standalone;
  }
  function dispatch(name,detail){
    try{window.dispatchEvent(new CustomEvent(name,{detail:detail||{}}));}catch(_){}
  }

  syncMode();
  window.addEventListener('online',function(){syncMode();dispatch('hermes:pwa-connection-change',{online:true});});
  window.addEventListener('offline',function(){syncMode();dispatch('hermes:pwa-connection-change',{online:false});});
  if(window.matchMedia){
    ['(display-mode: standalone)','(display-mode: fullscreen)','(display-mode: window-controls-overlay)'].forEach(function(query){
      try{
        var media=window.matchMedia(query);
        var handler=function(){syncMode();};
        if(media.addEventListener)media.addEventListener('change',handler);
        else if(media.addListener)media.addListener(handler);
      }catch(_){}
    });
  }

  window.addEventListener('beforeinstallprompt',function(event){
    event.preventDefault();
    window.hermesDeferredInstallPrompt=event;
    root.classList.add('pwa-installable');
    dispatch('hermes:pwa-installable');
  });
  window.addEventListener('appinstalled',function(){
    window.hermesDeferredInstallPrompt=null;
    root.classList.remove('pwa-installable');
    root.classList.add('pwa-installed');
    dispatch('hermes:pwa-installed');
  });
  document.addEventListener('visibilitychange',function(){
    if(document.visibilityState==='visible'){
      syncMode();
      root.classList.add('pwa-resumed');
      window.setTimeout(function(){root.classList.remove('pwa-resumed');},1200);
    }
  });

  window.HermesPWA={
    isStandalone:isStandalone,
    syncMode:syncMode,
    launchAction:function(){
      try{return new URLSearchParams(window.location.search||'').get('action')||null;}catch(_){return null;}
    },
    promptInstall:function(){
      var prompt=window.hermesDeferredInstallPrompt;
      if(!prompt||typeof prompt['prompt']!=='function')return Promise.resolve({outcome:'unavailable'});
      window.hermesDeferredInstallPrompt=null;
      root.classList.remove('pwa-installable');
      prompt['prompt']();
      return Promise.resolve(prompt.userChoice).catch(function(){return {outcome:'dismissed'};});
    }
  };
})();
