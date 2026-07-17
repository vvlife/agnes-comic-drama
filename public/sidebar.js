/* =========================================================
   统一左侧菜单栏组件（sidebar.js）
   被 index.html / editor.html 共用。
   - 菜单按「漫剧生成器」单一分组组织，整体按创作流程排序；
     剪辑器作为次级菜单（可展开）挂在生成器分组末尾。
   - 根据当前页面（editor / gen）自动决定菜单项行为：
       · 同页功能 -> 滚动定位 / 调用本页函数 / 切换步骤 / 打开弹窗
       · 跨页功能 -> 跳转并携带当前项目与锚点
   - 折叠状态存 localStorage，跨页面保持一致。
   ========================================================= */
(function(){
  "use strict";

  // 菜单配置：整体按创作流程排序；剪辑器作为次级菜单（children）
  var SB_CONFIG = {
    groups: [
      { title: "漫剧生成器", items: [
        { id:"gen-home", scope:"gen", icon:"🏠", label:"生成器首页", hash:"" },
        { id:"tab0",     scope:"gen", icon:"📝", label:"脚本创作",   tab:0, hash:"tab0" },
        { id:"tab1",     scope:"gen", icon:"🎨", label:"角色卡",     tab:1, hash:"tab1" },
        { id:"tab2",     scope:"gen", icon:"🖼️", label:"分镜与视频", tab:2, hash:"tab2" },
        { id:"editor",   scope:"editor", icon:"✂️", label:"剪辑器", children: [
          { id:"preview",  scope:"editor", icon:"📺", label:"预览监视器",   scroll:"preview" },
          { id:"timeline", scope:"editor", icon:"🎞️", label:"多轨时间轴",   scroll:"timeline" },
          { id:"inspector",scope:"editor", icon:"🔍", label:"片段检查器",   scroll:"inspector" },
          { id:"export",   scope:"editor", icon:"🎬", label:"重新剪辑导出", fn:"exportEdit" },
          { id:"ai",       scope:"editor", icon:"🤖", label:"AI 剪辑助手",  fn:"aiToggle" }
        ]},
        { id:"auto",     scope:"gen", icon:"🚀", label:"自动模式",   modal:"openAutoModal",     hash:"auto" },
        { id:"projects", scope:"gen", icon:"📂", label:"我的项目",   modal:"openProjectsModal", hash:"projects" },
        { id:"settings", scope:"gen", icon:"⚙️", label:"设置",       modal:"openSettingsModal", hash:"settings" }
      ]}
    ]
  };

  function getPid(){
    return window.__PROJECT_ID__ || (new URLSearchParams(location.search).get("project")) || "";
  }
  function curPage(){
    return location.pathname.indexOf("editor.html") >= 0 ? "editor" : "gen";
  }
  function findItem(id){
    for(var g=0; g<SB_CONFIG.groups.length; g++){
      var items = SB_CONFIG.groups[g].items;
      for(var i=0; i<items.length; i++){
        if(items[i].id===id) return items[i];
        if(items[i].children){
          for(var c=0; c<items[i].children.length; c++){
            if(items[i].children[c].id===id) return items[i].children[c];
          }
        }
      }
    }
    return null;
  }
  function setActive(btn){
    var all = document.querySelectorAll(".nav-item[data-sb]");
    for(var i=0;i<all.length;i++) all[i].classList.remove("active");
    if(btn) btn.classList.add("active");
  }
  function goto(url){ location.href = url; }

  // 渲染单个叶子项
  function leafHtml(it, page, isChild){
    var isLocal = (it.scope === page);
    var tgt = (isLocal && it.scroll) ? ' data-target="'+it.scroll+'"' : "";
    return '<button class="nav-item'+(isChild?" nav-child":"")+'" data-sb="'+it.id+'"'+tgt+'>'+
      '<span class="ic">'+it.icon+'</span><span class="lbl">'+it.label+'</span></button>';
  }

  // 渲染项（支持 children 次级菜单）
  function itemHtml(it, page){
    if(!it.children) return leafHtml(it, page, false);
    var isHome = (it.scope === page);
    var html = '<button class="nav-item nav-parent" data-sb="'+it.id+'"'+
      (isHome?' data-parent="'+it.id+'"':'')+'>'+
      '<span class="ic">'+it.icon+'</span><span class="lbl">'+it.label+'</span>'+
      (isHome?'<span class="caret">▸</span>':'')+'</button>';
    html += '<div class="nav-sub" data-sub="'+it.id+'">';
    for(var c=0;c<it.children.length;c++){ html += leafHtml(it.children[c], page, true); }
    html += '</div>';
    return html;
  }

  function initSidebar(){
    var page = curPage();
    var collapsed = (localStorage.getItem("sb-collapsed")==="1");
    if(collapsed) document.body.classList.add("nav-collapsed");

    var html = '<nav class="sidebar'+(collapsed?" collapsed":"")+'" id="sb-root">';
    html += '<div class="brand"><span class="lg">🎭</span><span class="lbl">Agnes 漫剧</span></div>';
    html += '<div class="nav">';
    for(var g=0; g<SB_CONFIG.groups.length; g++){
      html += '<div class="nav-group">'+SB_CONFIG.groups[g].title+'</div>';
      var items = SB_CONFIG.groups[g].items;
      for(var i=0; i<items.length; i++){ html += itemHtml(items[i], page); }
    }
    html += '</div>';
    html += '<div class="nav-foot"><button class="nav-toggle" id="sb-toggle">'+
            '<span class="ic" id="sb-toggle-ic">'+(collapsed?"⟩":"⟨")+'</span>'+
            '<span class="lbl">'+(collapsed?"展开":"收起")+'</span></button></div>';
    html += '</nav>';

    document.body.insertAdjacentHTML("afterbegin", html);
    bindSidebar(page);
  }

  function bindSidebar(page){
    var items = document.querySelectorAll(".nav-item[data-sb]");
    for(var i=0;i<items.length;i++){
      (function(btn){
        if(btn.dataset.parent){ // 次级菜单父节点：仅展开/折叠
          btn.addEventListener("click", function(){
            var sub = document.querySelector('.nav-sub[data-sub="'+btn.dataset.parent+'"]');
            if(sub) sub.classList.toggle("expanded");
            btn.classList.toggle("expanded");
          });
        } else {
          btn.addEventListener("click", function(){ runItem(btn.dataset.sb, page); });
        }
      })(items[i]);
    }
    var tg = document.getElementById("sb-toggle");
    if(tg) tg.addEventListener("click", toggleSidebar);

    // 当前在编辑器页时，高亮「剪辑器」父节点
    if(page==="editor"){
      var ep = document.querySelector('.nav-parent[data-parent="editor"]');
      if(ep) setActive(ep);
    }

    // 滚动时高亮当前可见区块（仅 editor 页的本地滚动项）
    if(page==="editor" && "IntersectionObserver" in window){
      var map = {};
      var local = document.querySelectorAll(".nav-item[data-target]");
      for(var j=0;j<local.length;j++){ map[local[j].dataset.target] = local[j]; }
      var io = new IntersectionObserver(function(entries){
        entries.forEach(function(e){
          if(e.isIntersecting && map[e.target.id]) setActive(map[e.target.id]);
        });
      }, { rootMargin:"-45% 0px -50% 0px", threshold:0 });
      ["preview","timeline","inspector","result"].forEach(function(id){
        var el = document.getElementById(id); if(el) io.observe(el);
      });
    }
  }

  function runItem(id, page){
    var it = findItem(id); if(!it) return;
    if(it.scope==="editor"){
      if(page==="editor"){
        if(it.scroll){ var el=document.getElementById(it.scroll); if(el) el.scrollIntoView({behavior:"smooth",block:"start"}); }
        else if(it.fn && window[it.fn]) window[it.fn]();
      } else {
        goto("/editor.html"+(getPid()?"?project="+encodeURIComponent(getPid()):""));
      }
    } else { // gen
      if(page==="gen"){
        if(it.id==="gen-home"){ if(window.showLanding) window.showLanding(); else window.scrollTo({top:0,behavior:"smooth"}); }
        else if(it.tab!=null){ if(window.enterWorkspace) window.enterWorkspace(it.tab); else if(window.switchTab) window.switchTab(it.tab); }
        else if(it.modal && window[it.modal]) window[it.modal]();
        else window.scrollTo({top:0, behavior:"smooth"});
      } else {
        goto("/"+(getPid()?"?project="+encodeURIComponent(getPid()):"")+(it.hash?"#"+it.hash:""));
      }
    }
  }

  function toggleSidebar(){
    var s = document.getElementById("sb-root");
    var collapsed = s.classList.toggle("collapsed");
    document.body.classList.toggle("nav-collapsed", collapsed);
    localStorage.setItem("sb-collapsed", collapsed?"1":"0");
    var ic = document.getElementById("sb-toggle-ic");
    var lbl = document.querySelector("#sb-toggle .lbl");
    if(ic) ic.textContent = collapsed?"⟩":"⟨";
    if(lbl) lbl.textContent = collapsed?"展开":"收起";
  }

  if(document.readyState!=="loading") initSidebar();
  else document.addEventListener("DOMContentLoaded", initSidebar);
})();
