/* Página do Cliente em abas (Unfold): lembra a aba aberta e abre "Backups" ao acompanhar uma operação. */
(function () {
  function abas() {
    return Array.prototype.slice.call(document.querySelectorAll('[x-data*="activeFieldsetTab"] nav a'));
  }
  function rotulo(a) {
    return a.textContent.trim().replace(/\s+\d+$/, '');  // remove o contador de erros do Unfold
  }
  function abrir(nome) {
    var alvo = abas().filter(function (a) { return rotulo(a) === nome; })[0];
    if (alvo) { alvo.click(); return true; }
    return false;
  }
  window.addEventListener('load', function () {
    setTimeout(function () {
      var q = new URLSearchParams(location.search);
      var consoleBackup = document.getElementById('bk-console');
      var emAndamento = consoleBackup && consoleBackup.getAttribute('data-ativo-id');
      var lembrada = null;
      try { lembrada = sessionStorage.getItem('cliente-aba'); } catch (e) { /* storage indisponível */ }

      if (q.get('acompanhar') || emAndamento) {
        abrir('Backups');
      } else if (lembrada) {
        abrir(lembrada);
      }
      abas().forEach(function (a) {
        a.addEventListener('click', function () {
          try { sessionStorage.setItem('cliente-aba', rotulo(a)); } catch (e) { /* ignora */ }
        });
      });
    }, 150);
  });
})();
