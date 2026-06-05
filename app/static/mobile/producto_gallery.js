(function () {
  "use strict";

  var root = document.getElementById("m-product-gallery");
  if (!root) return;

  var track = root.querySelector(".m-gallery__track");
  var dotsEl = root.querySelector(".m-gallery__dots");
  var imgs360 = [];
  try {
    imgs360 = JSON.parse(root.getAttribute("data-360") || "[]");
  } catch (_e) {
    imgs360 = [];
  }

  var slides = root.querySelectorAll(".m-gallery__slide");
  var lightbox = document.getElementById("m-gallery-lightbox");
  var lightboxImg = document.getElementById("m-gallery-lightbox-img");
  var viewer360 = document.getElementById("m-gallery-360");
  var viewer360Img = document.getElementById("m-gallery-360-img");
  var frame360 = 0;
  var interval360 = null;
  var rotating360 = false;
  var startX360 = 0;

  function setActiveDot(idx) {
    if (!dotsEl) return;
    dotsEl.querySelectorAll(".m-gallery__dot").forEach(function (dot, i) {
      dot.classList.toggle("m-gallery__dot--active", i === idx);
    });
  }

  function activeIndex() {
    if (!track || !slides.length) return 0;
    var left = track.scrollLeft;
    var w = track.clientWidth || 1;
    return Math.round(left / w);
  }

  function onScroll() {
    setActiveDot(activeIndex());
  }

  if (track) {
    track.addEventListener("scroll", onScroll, { passive: true });
  }

  function openLightbox(src, alt) {
    if (!lightbox || !lightboxImg) return;
    lightboxImg.src = src;
    lightboxImg.alt = alt || "";
    lightbox.hidden = false;
    document.body.classList.add("m-gallery-open");
  }

  function closeLightbox() {
    if (!lightbox) return;
    lightbox.hidden = true;
    document.body.classList.remove("m-gallery-open");
  }

  function detener360Auto() {
    if (interval360) {
      clearInterval(interval360);
      interval360 = null;
    }
  }

  function iniciar360Auto() {
    if (!viewer360Img || imgs360.length < 2) return;
    detener360Auto();
    frame360 = 0;
    viewer360Img.src = imgs360[0];
    interval360 = setInterval(function () {
      frame360 = (frame360 + 1) % imgs360.length;
      viewer360Img.src = imgs360[frame360];
    }, 90);
  }

  function close360() {
    detener360Auto();
    rotating360 = false;
    if (viewer360) viewer360.hidden = true;
    document.body.classList.remove("m-gallery-open");
  }

  function open360() {
    if (!viewer360 || !viewer360Img || !imgs360.length) return;
    closeLightbox();
    viewer360.hidden = false;
    document.body.classList.add("m-gallery-open");
    frame360 = 0;
    viewer360Img.src = imgs360[0];
    iniciar360Auto();
  }

  root.querySelectorAll("[data-gallery-zoom]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var src = btn.getAttribute("data-src") || "";
      var alt = btn.getAttribute("data-alt") || "";
      if (src) openLightbox(src, alt);
    });
  });

  root.querySelectorAll("[data-gallery-360]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      open360();
    });
  });

  if (lightbox) {
    lightbox.querySelectorAll("[data-gallery-close]").forEach(function (el) {
      el.addEventListener("click", closeLightbox);
    });
  }

  if (viewer360) {
    viewer360.querySelectorAll("[data-gallery-close]").forEach(function (el) {
      el.addEventListener("click", close360);
    });

    if (viewer360Img) {
      viewer360Img.addEventListener("mousedown", function (e) {
        detener360Auto();
        rotating360 = true;
        startX360 = e.clientX;
      });
      viewer360Img.addEventListener(
        "touchstart",
        function (e) {
          detener360Auto();
          rotating360 = true;
          startX360 = e.touches[0].clientX;
        },
        { passive: true }
      );
    }

    document.addEventListener("mouseup", function () {
      rotating360 = false;
    });
    document.addEventListener("touchend", function () {
      rotating360 = false;
    });
    document.addEventListener("mousemove", function (e) {
      if (!rotating360 || !viewer360Img || imgs360.length < 2) return;
      var diff = e.clientX - startX360;
      if (Math.abs(diff) > 12) {
        frame360 += diff > 0 ? -1 : 1;
        if (frame360 >= imgs360.length) frame360 = 0;
        if (frame360 < 0) frame360 = imgs360.length - 1;
        viewer360Img.src = imgs360[frame360];
        startX360 = e.clientX;
      }
    });
    document.addEventListener(
      "touchmove",
      function (e) {
        if (!rotating360 || !viewer360Img || imgs360.length < 2) return;
        var diff = e.touches[0].clientX - startX360;
        if (Math.abs(diff) > 12) {
          frame360 += diff > 0 ? -1 : 1;
          if (frame360 >= imgs360.length) frame360 = 0;
          if (frame360 < 0) frame360 = imgs360.length - 1;
          viewer360Img.src = imgs360[frame360];
          startX360 = e.touches[0].clientX;
        }
      },
      { passive: true }
    );
  }

  setActiveDot(0);
})();
