/* Página do Cliente: confirma antes de salvar um status que tira o site do ar ou o religa.
   Os grupos espelham Cliente.STATUS_NO_AR e Cliente.STATUS_FORA_DO_AR. */
(function () {
  var NO_AR = ['ativo', 'trial', 'atualizando'];
  var FORA_DO_AR = ['suspenso', 'cancelado', 'trial_expirado'];

  function statusInicial(campo) {
    var opcao = Array.prototype.filter.call(campo.options, function (o) { return o.defaultSelected; })[0];
    return opcao ? opcao.value : campo.value;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var campo = document.getElementById('id_status');
    if (!campo || !campo.form) return;
    var inicial = statusInicial(campo);
    campo.form.addEventListener('submit', function (evento) {
      var novo = campo.value;
      var mensagem = null;
      if (NO_AR.indexOf(inicial) !== -1 && FORA_DO_AR.indexOf(novo) !== -1) {
        mensagem = 'Este status tira o site e o cron do cliente do ar. Os dados são mantidos. Confirma?';
      } else if (FORA_DO_AR.indexOf(inicial) !== -1 && NO_AR.indexOf(novo) !== -1) {
        mensagem = 'Este status religa o site e o cron do cliente. Confirma?';
      }
      if (mensagem && !window.confirm(mensagem)) evento.preventDefault();
    });
  });
})();
