(async()=>{
  const wait=async(test)=>{const end=Date.now()+5000;while(Date.now()<end){if(test())return;await new Promise(r=>setTimeout(r,50))}throw Error('Control did not reach expected state')};
  const check=(ok,message)=>{if(!ok)throw Error(message)};
  const create=async name=>{document.getElementById('create').click();document.getElementById('new-name').value=name;document.getElementById('new-persona').value='本地窗口测试';document.getElementById('create-form').requestSubmit();await wait(()=>!working&&data.bot?.name===name)};
  try{
    await wait(()=>data!==null);
    await create('桌面测试');
    document.getElementById('remove-bot').click();await wait(()=>!working&&data.bots.length===1);
    document.getElementById('remove-bot').click();await wait(()=>!working&&data.bots.length===0);
    check(!document.getElementById('empty-bots').hidden&&document.getElementById('start').disabled,'Empty robot view failed');
    await create('重新创建');
    document.querySelector('[data-view="plugins"]').click();
    for(const name of ['comfyui','voicestudio']){
      document.querySelector(`[data-resource="${name}"][data-action="remove"]`).click();
      await wait(()=>!working&&data.plugins[name].removed);
      const add=document.querySelector(`[data-resource="${name}"][data-action="add"]`);
      check(!add.hidden&&add.closest('article').querySelector('.plugin-body').hidden,'Removed plugin still active');
      add.click();await wait(()=>!working&&!data.plugins[name].removed);
    }
    check(document.documentElement.scrollWidth<=innerWidth,'Horizontal overflow');
    window.__desktopResult={passed:true,robot_create_remove_empty:true,plugin_remove_restore:true,layout:true};
  }catch(error){window.__desktopResult={passed:false,error:String(error)}}
})();
