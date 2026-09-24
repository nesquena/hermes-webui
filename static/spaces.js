// Capy Spaces foundation shell.
// The first implementation slice only exposes safe metadata and recovery state.
(function(){
  async function fetchSpacesJson(path){
    const res = await fetch(path, {cache: 'no-store'});
    if (!res.ok) throw new Error('Spaces request failed: '+res.status);
    return res.json();
  }

  async function loadCapySpaces(){
    const root = document.getElementById('capySpacesRoot');
    if (!root) return;
    try {
      const data = await fetchSpacesJson('api/spaces');
      if (!data.enabled) {
        root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces disabled</h3><div class="capy-spaces-muted">Set HERMES_WEBUI_SPACES_ENABLED=1 to enable the foundation shell.</div></div>';
        return;
      }
      const spaces = data.spaces || [];
      root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces</h3><div class="capy-spaces-muted">'+spaces.length+' space(s). Widget rendering is intentionally disabled in the foundation slice.</div></div>' +
        spaces.map(function(s){
          return '<div class="capy-spaces-card" data-space-id="'+escapeHtml(s.space_id||'')+'"><strong>'+escapeHtml(s.name||s.space_id||'Untitled')+'</strong><div class="capy-spaces-muted">Widgets: '+(s.widget_count||0)+' · Revision: '+escapeHtml(s.revision_event_id||'none')+'</div></div>';
        }).join('');
    } catch (err) {
      root.innerHTML = '<div class="capy-spaces-card"><h3>Capy Spaces unavailable</h3><div class="capy-spaces-muted">'+escapeHtml(err.message||String(err))+'</div></div>';
    }
  }

  async function loadCapySpacesRecovery(){
    const root = document.getElementById('capySpacesRecovery');
    if (!root) return;
    try {
      const data = await fetchSpacesJson('api/spaces/recovery');
      root.textContent = data.enabled
        ? 'Safe recovery available. Generated widgets rendered here: '+String(!!data.generated_widgets_rendered)
        : 'Capy Spaces recovery is disabled because Spaces are disabled.';
    } catch (err) {
      root.textContent = 'Safe recovery unavailable: '+(err.message||String(err));
    }
  }

  function escapeHtml(value){
    return String(value).replace(/[&<>'"]/g, function(ch){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[ch];
    });
  }

  window.loadCapySpaces = loadCapySpaces;
  window.loadCapySpacesRecovery = loadCapySpacesRecovery;
  window.addEventListener('DOMContentLoaded', function(){
    loadCapySpaces();
    loadCapySpacesRecovery();
  });
})();
