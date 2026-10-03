"""Console ao vivo de backup/restauração na tela do Cliente (HTML + JS prontos para inserir).

O console faz polling do endpoint `registry_cliente_backup_status` e mostra o log da operação,
o tempo decorrido e um indicador de estado baseado no heartbeat (`sem_atividade_s`):

    < 30 s sem atividade   ● Executando
    >= 30 s                ⚠ possível travamento (ou nenhum worker pegou a tarefa)
    >= 120 s               ✖ provavelmente travado → botão Interromper

Abre sozinho quando há operação em andamento ou quando a URL traz `?acompanhar=<id>[&op=restauracao]`.
"""
from django.urls import reverse
from django.utils.html import escape

LIMITE_ALERTA_S = 30
LIMITE_TRAVADO_S = 120

_HTML = """
<style>
  @keyframes bkpulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }
  #bk-console { display:none; margin:14px 0 6px; border:1px solid #1e293b; border-radius:10px; overflow:hidden; background:#0b1220; }
  #bk-console .bk-cab { display:flex; flex-wrap:wrap; gap:10px; align-items:center; padding:8px 12px; background:#111b2e; color:#e2e8f0; font-size:13px; }
  #bk-console .bk-titulo { font-weight:700; }
  #bk-console .bk-estado { font-weight:600; }
  #bk-console .bk-tempo { margin-left:auto; font-variant-numeric:tabular-nums; color:#94a3b8; }
  #bk-console button { background:#1e293b; color:#e2e8f0; border:1px solid #334155; border-radius:6px; padding:3px 10px; font-size:12px; cursor:pointer; }
  #bk-console button:hover { background:#334155; }
  #bk-console .bk-int { background:#7f1d1d; border-color:#b91c1c; }
  #bk-console pre { margin:0; padding:12px 14px; max-height:340px; min-height:120px; overflow:auto; color:#cbd5e1; font:12px/1.55 Consolas, 'Courier New', monospace; white-space:pre-wrap; }
  #bk-console .bk-aviso { padding:8px 14px; font-size:12px; background:#1c1917; color:#fbbf24; border-top:1px solid #334155; display:none; }
</style>
<div id="bk-console" data-status="__STATUS__" data-interromper="__INTERROMPER__" data-ativo-id="__ATIVO_ID__" data-ativo-op="__ATIVO_OP__">
  <div class="bk-cab">
    <span class="bk-titulo" id="bk-titulo">Backup</span>
    <span class="bk-estado" id="bk-estado">● conectando…</span>
    <span class="bk-tempo" id="bk-tempo">00:00</span>
    <button type="button" id="bk-copiar">Copiar</button>
    <button type="button" id="bk-scroll">Pausar rolagem</button>
    <button type="button" id="bk-int" class="bk-int" style="display:none;">Interromper</button>
    <button type="button" id="bk-atualizar" style="display:none;">Atualizar lista</button>
  </div>
  <pre id="bk-log">Aguardando as primeiras mensagens…</pre>
  <div class="bk-aviso" id="bk-aviso"></div>
</div>
<script>
(function(){
  var raiz = document.getElementById("bk-console");
  if(!raiz) return;
  var q = new URLSearchParams(location.search);
  var id = q.get("acompanhar") || raiz.getAttribute("data-ativo-id");
  var op = q.get("op") || raiz.getAttribute("data-ativo-op") || "backup";
  if(!id) return;
  raiz.style.display = "block";
  var ALERTA = __ALERTA__, TRAVADO = __TRAVADO__;
  var urlStatus = raiz.getAttribute("data-status").replace("/backups/0/", "/backups/" + id + "/") + "?op=" + op;
  var urlInt = raiz.getAttribute("data-interromper").replace("/backups/0/", "/backups/" + id + "/");
  function $(i){ return document.getElementById(i); }
  function fmt(s){ var m = Math.floor(s/60), r = s % 60; return (m<10?"0":"") + m + ":" + (r<10?"0":"") + r; }
  $("bk-titulo").textContent = op === "restauracao" ? "Restauração" : "Backup";
  var pausado = false, ultimoLog = null, base = 0, t0 = Date.now(), ativo = true, tickTimer = null, pollTimer = null, extra = false, vimosAtiva = false;

  $("bk-scroll").onclick = function(){ pausado = !pausado; this.textContent = pausado ? "Retomar rolagem" : "Pausar rolagem"; };
  $("bk-copiar").onclick = function(){
    var t = $("bk-log").textContent, b = this;
    function ok(){ b.textContent = "Copiado!"; setTimeout(function(){ b.textContent = "Copiar"; }, 1500); }
    if(navigator.clipboard){ navigator.clipboard.writeText(t).then(ok); } else { var a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); document.execCommand("copy"); a.remove(); ok(); }
  };
  function atualizarTabela(){
    // busca a página de novo e troca só a tabela: o log do console continua visível
    return fetch(location.href, {credentials: "same-origin"}).then(function(r){ return r.text(); }).then(function(html){
      var novo = new DOMParser().parseFromString(html, "text/html").getElementById("bk-tabela");
      var atual = document.getElementById("bk-tabela");
      if(novo && atual){ atual.innerHTML = novo.innerHTML; return true; }
      return false;
    }).catch(function(){ return false; });
  }
  $("bk-atualizar").onclick = function(){
    atualizarTabela().then(function(ok){ if(!ok){ location.href = location.pathname; } });
  };
  $("bk-int").onclick = function(){
    if(!confirm("Interromper esta operação? Use apenas se ela realmente travou.")) return;
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    fetch(urlInt, {method: "POST", credentials: "same-origin", headers: {"X-CSRFToken": m ? m[1] : ""}}).then(function(){ poll(); });
  };

  function estado(txt, cor, pulsa){
    var e = $("bk-estado"); e.textContent = txt; e.style.color = cor;
    e.style.animation = pulsa ? "bkpulse 1.2s ease-in-out infinite" : "none";
  }
  function render(d){
    var pre = $("bk-log");
    if(d.log !== ultimoLog){
      ultimoLog = d.log;
      pre.textContent = d.log || "Aguardando as primeiras mensagens…";
      if(!pausado) pre.scrollTop = pre.scrollHeight;
    }
    base = d.decorrido_s; t0 = Date.now(); ativo = d.ativo_operacao;
    if(d.ativo_operacao){ vimosAtiva = true; }
    var aviso = $("bk-aviso"), btnInt = $("bk-int");
    aviso.style.display = "none"; btnInt.style.display = "none";
    if(d.ativo_operacao){
      $("bk-atualizar").style.display = "none";
      if(d.sem_atividade_s >= TRAVADO){
        estado("✖ Sem atividade há " + fmt(d.sem_atividade_s) + " — provavelmente travado", "#f87171", false);
        btnInt.style.display = "inline-block";
      } else if(d.sem_atividade_s >= ALERTA){
        estado("⚠ Sem atividade há " + d.sem_atividade_s + "s — possível travamento", "#fbbf24", false);
      } else {
        estado("● Executando", "#4ade80", true);
      }
      if(d.sem_atividade_s >= ALERTA && d.sem_inicio){
        aviso.textContent = "Nenhuma tarefa começou ainda: nenhum worker pegou o pedido. Confira se o serviço celery (manager) está rodando (docker service ps <stack>_celery).";
        aviso.style.display = "block";
      } else if(d.sem_atividade_s >= ALERTA && d.aguardando_worker){
        aviso.textContent = "Nenhum worker do nó iniciou a tarefa ainda. Confira se o serviço agente-no está rodando nesse nó (docker service ps <stack>_agente-no).";
        aviso.style.display = "block";
      }
    } else {
      $("bk-atualizar").style.display = "inline-block";
      if(d.status === "erro" && d.operacao === "backup"){ estado("✖ Erro", "#f87171", false); if(d.mensagem){ aviso.textContent = d.mensagem; aviso.style.display = "block"; } }
      else {
        estado("✔ Concluído", "#4ade80", false);
        if(d.mensagem){ aviso.textContent = "Avisos: " + d.mensagem; aviso.style.display = "block"; }
      }
    }
    $("bk-tempo").textContent = fmt(d.decorrido_s);
  }
  function poll(){
    fetch(urlStatus, {credentials: "same-origin"}).then(function(r){ return r.json(); }).then(function(d){
      render(d);
      if(!d.ativo_operacao){
        if(!extra){ extra = true; pollTimer = setTimeout(poll, 1500); } // uma consulta extra pega as últimas linhas
        else {
          clearInterval(tickTimer);
          if(vimosAtiva){ atualizarTabela(); }  // a lista passa a mostrar o resultado sem recarregar a página
        }
      } else { extra = false; pollTimer = setTimeout(poll, 1500); }
    }).catch(function(){ estado("⚠ Falha ao consultar o servidor — tentando de novo…", "#fbbf24", false); pollTimer = setTimeout(poll, 3000); });
  }
  tickTimer = setInterval(function(){ if(ativo){ $("bk-tempo").textContent = fmt(base + Math.floor((Date.now() - t0)/1000)); } }, 1000);
  poll();
})();
</script>
"""


def renderizar(cliente_pk, ativo_id=None, ativo_op='backup'):
    """HTML + JS do console. `ativo_id`/`ativo_op` abrem o console sozinho (operação em andamento)."""
    return (
        _HTML
        .replace('__STATUS__', escape(reverse('admin:registry_cliente_backup_status', args=[cliente_pk, 0])))
        .replace('__INTERROMPER__', escape(reverse('admin:registry_cliente_backup_interromper', args=[cliente_pk, 0])))
        .replace('__ATIVO_ID__', str(ativo_id or ''))
        .replace('__ATIVO_OP__', ativo_op)
        .replace('__ALERTA__', str(LIMITE_ALERTA_S))
        .replace('__TRAVADO__', str(LIMITE_TRAVADO_S))
    )
