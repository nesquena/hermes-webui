// Refresh installation identity when returning to an existing tab. No timer or
// title observer: each title owner keeps its native rules from instance_name.
(function(){
  let checking=false;
  async function refresh(){
    if(checking||document.visibilityState==='hidden')return;
    const initial=window.__HERMES_INSTANCE_BRANDING__;
    if(!initial||!initial.effective)return;
    checking=true;
    try{
      const response=await fetch(new URL('api/instance-branding',document.baseURI),{cache:'no-store',credentials:'same-origin'});
      if(response.ok){
        const current=await response.json();
        if(current.api_version===1&&current.effective&&current.effective.revision!==initial.effective.revision)location.reload();
      }
    }catch(_){/* Offline tabs retain their last shell until a later return. */}
    finally{checking=false;}
  }
  window.addEventListener('pageshow',refresh);
  document.addEventListener('visibilitychange',refresh);
})();
