(function () {
  "use strict";

  function toggleAccordion(header, open) {
    var root = header.closest("[data-accordion]");
    if (!root) return;
    var body = root.querySelector(":scope > .m-accordion__body");
    if (!body) return;
    var expanded = open != null ? open : header.getAttribute("aria-expanded") !== "true";
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
    root.classList.toggle("m-accordion--open", expanded);
  }

  function initAccordions() {
    document.querySelectorAll("[data-accordion] > .m-accordion__header").forEach(function (header) {
      var root = header.closest("[data-accordion]");
      var openDefault = root && root.getAttribute("data-open-default") === "true";
      if (openDefault) {
        toggleAccordion(header, true);
      }
      header.addEventListener("click", function () {
        toggleAccordion(header);
      });
    });
  }

  function initMovMore() {
    var btn = document.querySelector("[data-mov-more]");
    if (!btn) return;
    btn.addEventListener("click", function () {
      document.querySelectorAll(".m-mov-item--extra").forEach(function (el) {
        el.hidden = false;
      });
      btn.hidden = true;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initAccordions();
    initMovMore();
  });
})();
