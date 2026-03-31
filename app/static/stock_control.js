/**
 * Stock Control & Traceability Utilities
 * Real-time inventory validation and product history display
 */

const StockControl = (function() {
  "use strict";

  // ============================================
  // STOCK VALIDATION
  // ============================================

  /**
   * Check if sale items have sufficient stock
   */
  function checkStockAvailability(items) {
    if (!items || items.length === 0) {
      return Promise.resolve({ success: false, message: "No items to validate" });
    }

    const payload = { items: items };

    return fetch("/ventas/api/stock/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    .then(res => res.json())
    .catch(err => ({
      success: false,
      message: "Error validando stock: " + err.message,
    }));
  }

  /**
   * Get stock details for a product
   */
  function getProductStock(codigo) {
    return fetch("/ventas/api/stock/product/" + encodeURIComponent(codigo))
      .then(res => res.json())
      .catch(err => ({
        success: false,
        message: "Error obteniendo stock: " + err.message,
      }));
  }

  /**
   * Validate a single item before sale
   */
  function validateSaleItem(codigo, marca, bodega, cantidad) {
    return getProductStock(codigo).then(resp => {
      if (!resp.success) {
        return { valid: false, message: "Producto no encontrado" };
      }

      const variants = resp.by_variant || [];
      const matching = variants.find(v =>
        v.marca === marca && v.bodega === bodega
      );

      if (!matching) {
        return {
          valid: false,
          message: `Stock no encontrado para ${codigo} - ${marca} - ${bodega}`,
        };
      }

      if (matching.stock < cantidad) {
        return {
          valid: false,
          message: `Stock insuficiente. Disponible: ${matching.stock}, Requerido: ${cantidad}`,
        };
      }

      return {
        valid: true,
        available: matching.stock,
        message: "",
      };
    });
  }

  // ============================================
  // PRODUCT TRACEABILITY
  // ============================================

  /**
   * Get full product history (ingresos, ventas, notas_credito, stock)
   */
  function getProductHistory(codigo) {
    return fetch("/ventas/api/product/history/" + encodeURIComponent(codigo))
      .then(res => res.json())
      .catch(err => ({
        success: false,
        message: "Error obteniendo historial: " + err.message,
      }));
  }

  /**
   * Get most recent sale for a product
   */
  function getLastSale(codigo) {
    return fetch("/ventas/api/product/last-sale/" + encodeURIComponent(codigo))
      .then(res => res.json())
      .catch(err => ({
        success: false,
      }));
  }

  /**
   * Format timestamp to readable date
   */
  function formatDate(isoString) {
    if (!isoString) return "-";
    const date = new Date(isoString);
    return date.toLocaleString("es-CL", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  /**
   * Create traceability HTML from history data
   */
  function formatTraceabilityHTML(history) {
    if (!history.success) {
      return `<div class="alert alert-error">Error: ${history.message}</div>`;
    }

    let html = `
      <div class="traceability-container">
        <div class="traceability-header">
          <h3>Trazabilidad Producto: ${history.codigo_producto}</h3>
        </div>

        <div class="traceability-tabs">
          <button class="tab-btn active" data-tab="resumen">Resumen</button>
          <button class="tab-btn" data-tab="ingresos">Ingresos (${history.ingresos?.length || 0})</button>
          <button class="tab-btn" data-tab="ventas">Ventas (${history.ventas?.length || 0})</button>
          <button class="tab-btn" data-tab="devoluciones">Devoluciones (${history.notas_credito?.length || 0})</button>
        </div>

        <div class="traceability-content">
          <!-- RESUMEN -->
          <div id="resumen" class="tab-pane active">
            <div class="resumen-grid">
              <div class="resumen-card">
                <h4>Stock Actual</h4>
                <div class="resumen-items">
    `;

    // Stock summary
    if (history.stock_summary && Object.keys(history.stock_summary).length > 0) {
      for (const [key, stock] of Object.entries(history.stock_summary)) {
        html += `
          <div class="stock-item">
            <span class="stock-warehouse">${stock.bodega}</span>
            <span class="stock-brand">${stock.marca}</span>
            <span class="stock-qty" style="font-weight: bold; color: #16a34a;">${stock.stock}</span>
          </div>
        `;
      }
    } else {
      html += `<p style="color: #666;">Sin stock disponible</p>`;
    }

    html += `</div></div>`;

    // Last sale
    if (history.last_sale) {
      html += `
        <div class="resumen-card">
          <h4>Última Venta</h4>
          <div class="last-sale-info">
            <p><strong>Cliente:</strong> ${history.last_sale.cliente}</p>
            <p><strong>Documento:</strong> ${history.last_sale.documento_tipo} #${history.last_sale.documento_numero || "-"}</p>
            <p><strong>Cantidad:</strong> ${history.last_sale.cantidad}</p>
            <p><strong>Fecha:</strong> ${formatDate(history.last_sale.fecha)}</p>
          </div>
        </div>
      `;
    }

    html += `</div>`;

    // INGRESOS tab
    html += `
      <div id="ingresos" class="tab-pane">
        <div class="traceability-timeline">
    `;
    if (history.ingresos && history.ingresos.length > 0) {
      history.ingresos.forEach(item => {
        html += `
          <div class="timeline-item ingreso">
            <div class="timeline-marker" style="background: #16a34a;">📥</div>
            <div class="timeline-content">
              <h5>${formatDate(item.timestamp)}</h5>
              <p><strong>Proveedor:</strong> ${item.proveedor || "-"}</p>
              <p><strong>RUT:</strong> ${item.proveedor_rut || "-"}</p>
              <p><strong>Factura:</strong> ${item.documento_numero || "-"}</p>
              <p><strong>Marca/Variante:</strong> ${item.marca || "-"}</p>
              <p><strong>Bodega:</strong> ${item.bodega || "-"}</p>
              <p class="qty"><strong>Cantidad:</strong> <span style="font-size: 18px; color: #16a34a;">${item.cantidad}</span></p>
            </div>
          </div>
        `;
      });
    } else {
      html += `<p style="color: #666; padding: 20px;">Sin ingresos registrados</p>`;
    }
    html += `</div></div>`;

    // VENTAS tab
    html += `
      <div id="ventas" class="tab-pane">
        <div class="traceability-timeline">
    `;
    if (history.ventas && history.ventas.length > 0) {
      history.ventas.forEach(item => {
        html += `
          <div class="timeline-item venta">
            <div class="timeline-marker" style="background: #dc2626;">📤</div>
            <div class="timeline-content">
              <h5>${formatDate(item.timestamp)}</h5>
              <p><strong>Cliente:</strong> ${item.cliente || "-"}</p>
              <p><strong>RUT:</strong> ${item.cliente_rut || "-"}</p>
              <p><strong>${item.documento_tipo}:</strong> ${item.documento_numero || "-"}</p>
              <p><strong>Marca/Variante:</strong> ${item.marca || "-"}</p>
              <p><strong>Bodega:</strong> ${item.bodega || "-"}</p>
              <p class="qty"><strong>Cantidad:</strong> <span style="font-size: 18px; color: #dc2626;">${item.cantidad}</span></p>
              ${item.precio_unitario ? `<p><strong>Precio Unit:</strong> $${parseFloat(item.precio_unitario).toLocaleString("es-CL")}</p>` : ""}
            </div>
          </div>
        `;
      });
    } else {
      html += `<p style="color: #666; padding: 20px;">Sin ventas registradas</p>`;
    }
    html += `</div></div>`;

    // DEVOLUCIONES tab
    html += `
      <div id="devoluciones" class="tab-pane">
        <div class="traceability-timeline">
    `;
    if (history.notas_credito && history.notas_credito.length > 0) {
      history.notas_credito.forEach(item => {
        html += `
          <div class="timeline-item devolucion">
            <div class="timeline-marker" style="background: #2563eb;">↩️</div>
            <div class="timeline-content">
              <h5>${formatDate(item.timestamp)}</h5>
              <p><strong>Nota Crédito:</strong> ${item.numero || "-"}</p>
              <p><strong>Documento Original:</strong> ${item.documento_original || "-"}</p>
              <p><strong>Cliente:</strong> ${item.cliente || "-"}</p>
              <p><strong>Razón:</strong> ${item.razon || "-"}</p>
              <p><strong>Marca/Variante:</strong> ${item.marca || "-"}</p>
              <p><strong>Bodega:</strong> ${item.bodega || "-"}</p>
              <p class="qty"><strong>Cantidad Devuelta:</strong> <span style="font-size: 18px; color: #2563eb;">${item.cantidad}</span></p>
            </div>
          </div>
        `;
      });
    } else {
      html += `<p style="color: #666; padding: 20px;">Sin devoluciones registradas</p>`;
    }
    html += `</div></div>`;

    html += `</div></div>`;

    return html;
  }

  /**
   * Open traceability modal for a product
   */
  function openTraceabilityModal(codigo, titulo) {
    const modal = document.getElementById("traceabilityModal");
    if (!modal) {
      console.error("Traceability modal not found in page");
      return;
    }

    const contenido = document.getElementById("traceabilityContenido");
    if (!contenido) return;

    // Show loading
    contenido.innerHTML = `
      <div style="text-align: center; padding: 40px;">
        <div class="spinner" style="display: inline-block;"></div>
        <p>Cargando trazabilidad...</p>
      </div>
    `;

    modal.style.display = "flex";

    // Fetch history
    getProductHistory(codigo).then(history => {
      contenido.innerHTML = formatTraceabilityHTML(history);

      // Attach tab switching
      const tabBtns = contenido.querySelectorAll(".tab-btn");
      const tabPanes = contenido.querySelectorAll(".tab-pane");

      tabBtns.forEach(btn => {
        btn.addEventListener("click", function(e) {
          const tabName = this.getAttribute("data-tab");

          // Remove active from all
          tabBtns.forEach(b => b.classList.remove("active"));
          tabPanes.forEach(p => p.classList.remove("active"));

          // Add active to clicked
          this.classList.add("active");
          document.getElementById(tabName).classList.add("active");
        });
      });
    });
  }

  /**
   * Close traceability modal
   */
  function closeTraceabilityModal() {
    const modal = document.getElementById("traceabilityModal");
    if (modal) {
      modal.style.display = "none";
    }
  }

  // ============================================
  // CREDIT NOTE
  // ============================================

  /**
   * Create a credit note for a sales document
   */
  function createCreditNote(documentoId, items, razon) {
    const payload = {
      documento_id: documentoId,
      items: items,
      razon: razon,
    };

    return fetch("/ventas/api/credit-note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    .then(res => res.json())
    .catch(err => ({
      success: false,
      message: "Error creando nota de crédito: " + err.message,
    }));
  }

  /**
   * Get credit notes for a sales document
   */
  function getCreditNotes(documentoId) {
    return fetch(`/ventas/api/credit-notes/documento/${documentoId}`)
      .then(res => res.json())
      .catch(err => ({
        success: false,
        message: "Error obteniendo notas de crédito: " + err.message,
      }));
  }

  // ============================================
  // PUBLIC API
  // ============================================

  return {
    checkStockAvailability: checkStockAvailability,
    getProductStock: getProductStock,
    validateSaleItem: validateSaleItem,
    getProductHistory: getProductHistory,
    getLastSale: getLastSale,
    formatTraceabilityHTML: formatTraceabilityHTML,
    openTraceabilityModal: openTraceabilityModal,
    closeTraceabilityModal: closeTraceabilityModal,
    createCreditNote: createCreditNote,
    getCreditNotes: getCreditNotes,
  };
})();

// Global aliases for easy access
window.openProductoTraceability = function(codigo, titulo) {
  StockControl.openTraceabilityModal(codigo, titulo || codigo);
};

window.closeProductoTraceability = function() {
  StockControl.closeTraceabilityModal();
};
