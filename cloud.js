/* Hosted UI adapter. Local dashboard behavior is untouched. */
(() => {
  window.DEV_SERVICES_CLOUD = true;
  const originalFetch = window.fetch.bind(window);
  let selected = localStorage.getItem('dev-services-agent') || '';
  const withAgent = path => {
    const url = new URL(path, location.origin);
    if (selected) url.searchParams.set('agent', selected);
    return url.pathname + url.search;
  };
  const cloud = async (path, body) => {
    const response = await originalFetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Dashboard':'1'},body:JSON.stringify(body)});
    if (response.status === 401) { location.assign('/auth'); throw Error('Please sign in.'); }
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Request failed.');
    return result;
  };
  window.fetch = async (input, options) => {
    if (typeof input !== 'string' || !input.startsWith('/api/') || input.startsWith('/api/cloud/')) return originalFetch(input, options);
    const response = await originalFetch(withAgent(input), options);
    if (response.status === 401) location.assign('/auth');
    if (response.status === 202) {
      const result = await response.clone().json();
      if (result.commandId) {
        for (let i=0;i<25;i++) {
          await new Promise(resolve=>setTimeout(resolve,1000));
          const reply=await cloud(withAgent('/api/cloud/result?id='+encodeURIComponent(result.commandId)));
          if (reply.state==='done') {
            if (reply.result?.error) throw Error(reply.result.error);
            return response;
          }
        }
        throw Error('Agent did not confirm the action. Check service status before trying again.');
      }
    }
    return response;
  };
  const OriginalEventSource=window.EventSource;
  window.EventSource=class extends OriginalEventSource {
    constructor(url,options) { super(withAgent(url),options); }
  };
  document.addEventListener('DOMContentLoaded',()=>{
    document.body.dataset.cloud='true';
    const toolbar=document.querySelector('.head-right');
    const select=document.createElement('select');
    select.setAttribute('aria-label','Paired computer'); select.style.cssText='width:auto;max-width:250px;padding:7px;font-size:12px';
    toolbar.prepend(select);
    const pair=document.createElement('button');pair.textContent='Pair computer';toolbar.insertBefore(pair,document.querySelector('#guide-open'));
    const revoke=document.createElement('button');revoke.textContent='Disconnect computer';toolbar.append(revoke);
    const dialog=document.createElement('dialog');
    dialog.innerHTML='<div class="dlg-head"><h3>Connect your computer</h3><button id="pair-close">Close</button></div><div class="form-body"><p>Configure your projects in the local dashboard, then run the agent on that computer.</p><p><a href="/downloads/agent.zip">Download the Python agent</a> · Python 3.10+ required</p><ol><li>Extract the ZIP and install dependencies: <code>python3 -m pip install -r requirements.txt</code>.</li><li>Run <code>python3 server.py</code> and add projects in its local dashboard.</li><li>In the same folder, run the pairing command below. Type PAIR and enter the one-time code when prompted.</li></ol><pre id="pair-command"></pre><p>One-time code · expires in 10 minutes</p><pre id="pair-code"></pre><p class="hint">Keep the agent running for remote controls. Pairing shares service status and requested logs with your account. Project commands are configured locally. Your hosted password is separate from this one-time code.</p></div>';
    document.body.append(dialog);
    dialog.querySelector('#pair-close').onclick=()=>dialog.close();
    pair.onclick=async()=>{try{const result=await cloud('/api/cloud/pair',{});dialog.querySelector('#pair-command').textContent='python3 agent.py --pair '+result.origin;dialog.querySelector('#pair-code').textContent=result.code;dialog.showModal()}catch(error){alert(error.message)}};
    document.addEventListener('click',event=>{if(event.target.closest('[data-cloud-pair]'))pair.click()});
    revoke.onclick=async()=>{if(!selected||!confirm('Disconnect this computer? Remote controls stop working until it is paired again. Local projects keep running.'))return;try{await cloud('/api/cloud/revoke',{id:selected});localStorage.removeItem('dev-services-agent');selected='';location.reload()}catch(error){alert(error.message)}};
    const localHelp=()=>{alert('Add or edit project commands in the local dashboard on the paired computer. Run python3 server.py in the agent folder to open it.');};
    document.querySelector('#add-service').onclick=()=>selected?localHelp():pair.click();
    document.querySelector('#guide-open').onclick=()=>pair.click();
    document.querySelector('#quit').textContent='Sign out';
    document.querySelector('#quit').onclick=async()=>{await cloud('/api/logout',{});location.assign('/auth')};
    select.onchange=()=>{selected=select.value;localStorage.setItem('dev-services-agent',selected);location.reload()};
    const update=async()=>{
      try {
        const result=await cloud('/api/cloud/agents');
        const agents=result.agents;
        if(!agents.some(a=>a.id===selected)) {selected=agents[0]?.id||'';localStorage.setItem('dev-services-agent',selected);}
        select.replaceChildren();
        if(!agents.length){const option=new Option('No paired computers','');select.add(option);}
        for(const agent of agents) select.add(new Option(agent.name+(agent.seen>Date.now()/1000-20?' · online':' · offline'),agent.id));
        select.value=selected;revoke.disabled=!selected;
        if(!agents.length){document.querySelector('#groups').innerHTML='<div class="empty"><h2>Your projects, connected.</h2><p>Pair your computer to see its services and control them from here.</p><button id="empty-pair" class="primary">Pair your first computer</button></div>';document.querySelector('#empty-pair').onclick=()=>pair.click();}
      } catch(error){document.querySelector('#conn').textContent=error.message;}
    };
    update();setInterval(update,4000);
  });
})();
