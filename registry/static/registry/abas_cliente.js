/* Página do Cliente em abas (Unfold): lembra a aba aberta e abre "Backups" ao acompanhar uma operação.
 *
 * São duas barras: a dos fieldsets (Cadastro, Acesso e Infraestrutura...) dentro de "General", e a
 * dos inlines (#tabs-items: General, Domínios, Logs...). As duas são lembradas; um inline com erro de
 * validação é aberto no lugar da aba lembrada, senão o erro fica escondido.
 */
(function () {
  function abas() {
    return Array.prototype.slice.call(document.querySelectorAll('[x-data*="activeFieldsetTab"] nav a'));
  }
  function abasInline() {
    return Array.prototype.slice.call(document.querySelectorAll('#tabs-items a[href^="#"]'));
  }
  function rotulo(a) {
    return a.textContent.trim().replace(/\s+\d+$/, '');  // remove o contador de erros do Unfold
  }
  function abrir(nome) {
    var alvo = abas().filter(function (a) { return rotulo(a) === nome; })[0];
    if (alvo) { alvo.click(); return true; }
    return false;
  }
  function abrirInline(href) {
    var alvo = abasInline().filter(function (a) { return a.getAttribute('href') === href; })[0];
    if (alvo) { alvo.click(); return true; }
    return false;
  }
  function inlineComErro() {
    return abasInline().filter(function (a) {
      var grupo = document.getElementById(a.getAttribute('href').slice(1) + '-group');
      return grupo && grupo.querySelector('.errorlist, .errornote, .errors');
    })[0];
  }
  function ler(chave) {
    try { return sessionStorage.getItem(chave); } catch (e) { return null; }  // storage indisponível
  }
  function gravar(chave, valor) {
    try { sessionStorage.setItem(chave, valor); } catch (e) { /* ignora */ }
  }
  window.addEventListener('load', function () {
    setTimeout(function () {
      var q = new URLSearchParams(location.search);
      var consoleBackup = document.getElementById('bk-console');
      var emAndamento = consoleBackup && consoleBackup.getAttribute('data-ativo-id');
      var lembrada = ler('cliente-aba');
      var inlineLembrada = ler('cliente-aba-inline');
      var erro = inlineComErro();

      if (q.get('acompanhar') || emAndamento) {
        abrirInline('#general');
        abrir('Backups');
      } else {
        if (lembrada) abrir(lembrada);
        if (erro) erro.click();
        else if (inlineLembrada) abrirInline(inlineLembrada);
      }
      abas().forEach(function (a) {
        a.addEventListener('click', function () { gravar('cliente-aba', rotulo(a)); });
      });
      abasInline().forEach(function (a) {
        a.addEventListener('click', function () { gravar('cliente-aba-inline', a.getAttribute('href')); });
      });
    }, 150);
  });
})();
