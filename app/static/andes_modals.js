/**
 * Cierra un overlay solo si mousedown y click fueron en el fondo.
 * Evita que seleccionar texto (o el autocompletado) y soltar fuera cierre el modal.
 */
(function (w) {
    "use strict";
    if (w.andesBindBackdropClose) return;

    w.andesBindBackdropClose = function (overlay, closeFn) {
        if (!overlay || typeof closeFn !== "function") return;
        if (overlay.getAttribute("data-andes-backdrop-bound") === "1") return;
        overlay.setAttribute("data-andes-backdrop-bound", "1");
        var pressedOnBackdrop = false;
        overlay.addEventListener("mousedown", function (e) {
            pressedOnBackdrop = e.target === overlay;
        });
        overlay.addEventListener("click", function (e) {
            var shouldClose = pressedOnBackdrop && e.target === overlay;
            pressedOnBackdrop = false;
            if (shouldClose) closeFn(e);
        });
    };
})(window);
