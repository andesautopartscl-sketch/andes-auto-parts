(function () {
  "use strict";

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderRecent() {
    var section = document.getElementById("m-home-recent");
    var list = document.getElementById("m-home-recent-list");
    if (!section || !list || !window.AndesOfflineDb) return;

    AndesOfflineDb.getRecentProducts(3).then(function (items) {
      if (!items.length) return;
      section.hidden = false;
      list.innerHTML = items
        .map(function (p) {
          var href = "/m/producto/" + encodeURIComponent(p.codigo || "");
          return (
            '<li><a href="' +
            href +
            '" class="m-home-recent__link">' +
            '<span class="m-home-recent__code">' +
            escapeHtml(p.codigo || "") +
            "</span>" +
            '<span class="m-home-recent__desc">' +
            escapeHtml(p.descripcion || "") +
            "</span></a></li>"
          );
        })
        .join("");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderRecent();
  });
})();
