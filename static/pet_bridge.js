(function(){
  const PET_NAVIGATION_LAST_KEY='hermes-pet-navigation-last-id';
  let pollBusy=false;

  function _petBridgeCsrfHeaders(){
    const token=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
    return token?{'X-Hermes-CSRF-Token':token}:{};
  }

  async function _petBridgeApi(path, options={}){
    const response=await fetch(path,{credentials:'include',cache:'no-store',...options});
    if(!response.ok) throw new Error(`Pet navigation failed: ${response.status}`);
    return response.json();
  }

  async function _ackPetNavigation(command){
    if(!command||!command.id) return false;
    try{
      await _petBridgeApi('/api/pet/navigation_ack',{method:'POST',headers:{'Content-Type':'application/json',..._petBridgeCsrfHeaders()},body:JSON.stringify({id:command.id})});
      return true;
    }catch(_){return false;}
  }

  async function _pollPetNavigation(){
    if(pollBusy) return;
    pollBusy=true;
    try{
      const since=(()=>{try{return localStorage.getItem(PET_NAVIGATION_LAST_KEY)||'';}catch(_){return '';}})();
      const data=await _petBridgeApi('/api/pet/navigation?since='+encodeURIComponent(since));
      const command=data&&data.command;
      if(command&&command.id&&command.id!==since&&typeof window.__hermesApplyPetNavigationCommand==='function'){
        const acked=await _ackPetNavigation(command);
        if(!acked) return;
        await window.__hermesApplyPetNavigationCommand(command);
        try{window.focus();}catch(_){}
        try{localStorage.setItem(PET_NAVIGATION_LAST_KEY,String(command.id));}catch(_){}
      }
    }catch(_e){
      // Desktop pet navigation is opportunistic; normal session polling remains authoritative.
    }finally{
      pollBusy=false;
    }
  }

  setTimeout(_pollPetNavigation,600);
  setInterval(_pollPetNavigation,1000);
})();
