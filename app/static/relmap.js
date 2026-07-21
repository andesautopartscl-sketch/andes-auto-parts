/**
 * Renderer compartido del mapa de relaciones (productos / embeds).
 * No modifica el renderer inline de Ventas ERP; convive de forma segura.
 */
(function (global) {
  'use strict';

  function esc(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function labelDocType(kind) {
    var map = {
      cotizacion: 'Cotización',
      orden_venta: 'Orden de venta',
      orden_compra: 'Orden de compra',
      factura: 'Factura de deudores',
      boleta: 'Boleta',
      factura_proveedor: 'Factura proveedor',
      nota_credito: 'Nota de crédito',
      picking: 'Picking bodega',
      socio_negocio: 'Socios de negocios',
      producto: 'Producto',
      ingreso: 'Ingreso de stock'
    };
    return map[String(kind || '').toLowerCase()] || String(kind || '').replace(/_/g, ' ');
  }

  function fmtMapMoney(value, type) {
    var t = String(type || '').toLowerCase();
    if (t === 'socio_negocio' || t === 'picking' || t === 'producto') return '';
    var n = Number(value || 0);
    if (!Number.isFinite(n)) return '';
    return '$' + Math.round(n).toLocaleString('es-CL');
  }

  function layoutRelationshipMap(nodes, edges) {
    var CARD_W = 156;
    var CARD_H = 104;
    var GAP_X = 46;
    var GAP_Y = 12;
    var PAD = 20;
    var list = Array.isArray(nodes) ? nodes.slice() : [];
    var links = Array.isArray(edges) ? edges.slice() : [];
    var byKey = {};
    list.forEach(function (n) { byKey[n.key] = n; });

    var children = {};
    var parents = {};
    links.forEach(function (e) {
      if (!byKey[e.from] || !byKey[e.to]) return;
      (children[e.from] = children[e.from] || []).push(e.to);
      (parents[e.to] = parents[e.to] || []).push(e.from);
    });

    var depth = {};
    var roots = list.filter(function (n) {
      return !(parents[n.key] && parents[n.key].length);
    });
    if (!roots.length && list.length) roots.push(list[0]);

    var queue = [];
    roots.forEach(function (n) {
      depth[n.key] = 0;
      queue.push(n.key);
    });
    while (queue.length) {
      var k = queue.shift();
      var d = depth[k] || 0;
      (children[k] || []).forEach(function (ck) {
        var nd = d + 1;
        if (depth[ck] == null || nd > depth[ck]) {
          depth[ck] = nd;
          queue.push(ck);
        }
      });
    }
    list.forEach(function (n) {
      if (depth[n.key] == null) depth[n.key] = 0;
    });

    var columns = {};
    list.forEach(function (n) {
      var col = depth[n.key] || 0;
      (columns[col] = columns[col] || []).push(n);
    });
    var colKeys = Object.keys(columns).map(Number).sort(function (a, b) { return a - b; });
    var maxRows = 1;
    colKeys.forEach(function (c) {
      columns[c].sort(function (a, b) {
        var ta = String(a.created_at || a.fecha || '');
        var tb = String(b.created_at || b.fecha || '');
        if (ta !== tb) return ta < tb ? -1 : 1;
        return (a.id || 0) - (b.id || 0);
      });
      maxRows = Math.max(maxRows, columns[c].length);
    });

    var positions = {};
    var totalH = maxRows * CARD_H + Math.max(0, maxRows - 1) * GAP_Y;
    colKeys.forEach(function (c, ci) {
      var colNodes = columns[c];
      var colH = colNodes.length * CARD_H + Math.max(0, colNodes.length - 1) * GAP_Y;
      var startY = PAD + Math.max(0, (totalH - colH) / 2);
      colNodes.forEach(function (n, ri) {
        positions[n.key] = {
          x: PAD + ci * (CARD_W + GAP_X),
          y: startY + ri * (CARD_H + GAP_Y),
          w: CARD_W,
          h: CARD_H
        };
      });
    });

    var width = PAD * 2 + Math.max(1, colKeys.length) * CARD_W + Math.max(0, colKeys.length - 1) * GAP_X;
    var height = PAD * 2 + totalH;
    return { positions: positions, width: width, height: height };
  }

  function buildMapCardHtml(node) {
    var t = String(node.type || '').toLowerCase();
    var isPartner = t === 'socio_negocio';
    var isProduct = t === 'producto';
    var title = node.label || labelDocType(t);
    var num = isPartner ? (node.number || '—') : (node.number || ('#' + (node.id || '')));
    var fecha = String(node.fecha || '').trim();
    var ref = String(node.ref_externa || node.cliente_nombre || '').trim();
    var money = isPartner || t === 'picking' || isProduct ? '' : fmtMapMoney(node.total, t);
    var status = String(node.status || '').trim();
    // Producto activo no necesita badge; ingresos sí muestran "Registrado" en verde
    if (isProduct || status === 'activo') status = '';
    var statusClass = '';
    if (/pagado|registrado|entregado|aprobada|procesada/i.test(status)) statusClass = ' is-ok';
    else if (/pendiente|anulad/i.test(status)) statusClass = ' is-warn';
    var pay = (t === 'factura' || t === 'boleta')
      ? (String(node.estado_pago || '').toLowerCase() === 'pagado' ? 'Pagado' : 'Pendiente cobro')
      : '';
    var classes = [
      'relmap-card',
      'tone-' + (node.badge_tone || 'slate'),
      node.is_current ? 'is-current' : '',
      node.locked ? 'is-locked' : '',
      isPartner ? 'is-partner' : '',
      isProduct ? 'is-product' : '',
      node.view_url ? 'is-clickable' : ''
    ].filter(Boolean).join(' ');

    var metaHtml = '';
    var stockLockOpen = null; // null = no product lock
    if (isProduct) {
      var desc = String(node.descripcion || node.ref_externa || '').trim();
      var marca = String(node.marca || '').trim();
      var stockNum = Number(node.stock);
      if (!Number.isFinite(stockNum)) stockNum = 0;
      var hasStock = stockNum > 0;
      stockLockOpen = hasStock;
      if (desc && desc !== num) {
        metaHtml += '<div class="relmap-card-line" title="' + esc(desc) + '">' + esc(desc) + '</div>';
      }
      metaHtml += '<dl class="relmap-card-meta">';
      if (marca) metaHtml += '<dt>Marca</dt><dd>' + esc(marca) + '</dd>';
      metaHtml += '<dt>Stock</dt><dd class="' + (hasStock ? 'is-stock-ok' : 'is-stock-out') + '">' + esc(String(stockNum)) + '</dd>';
      metaHtml += '</dl>';
      status = hasStock ? 'Con stock' : 'Sin stock';
      statusClass = hasStock ? ' is-ok' : ' is-out';
    } else if (!isPartner) {
      metaHtml = '<dl class="relmap-card-meta">';
      if (fecha) metaHtml += '<dt>Fecha</dt><dd>' + esc(fecha) + '</dd>';
      if (ref) metaHtml += '<dt>Ref.</dt><dd title="' + esc(ref) + '">' + esc(ref) + '</dd>';
      metaHtml += '</dl>';
    } else if (isPartner && (node.cliente_nombre || ref)) {
      metaHtml = '<div class="relmap-card-line">' + esc(node.cliente_nombre || ref) + '</div>';
    }

    var lockSvgClosed =
      '<svg viewBox="0 0 16 16" width="11" height="11" focusable="false">' +
        '<path fill="currentColor" d="M8 1.5A3.5 3.5 0 0 0 4.5 5v2H3.2c-.66 0-1.2.54-1.2 1.2v5.1c0 .66.54 1.2 1.2 1.2h9.6c.66 0 1.2-.54 1.2-1.2V8.2c0-.66-.54-1.2-1.2-1.2H11.5V5A3.5 3.5 0 0 0 8 1.5zm0 1.6c1.05 0 1.9.85 1.9 1.9v2H6.1V5c0-1.05.85-1.9 1.9-1.9z"/>' +
      '</svg>';
    /* Candado abierto: aro suelto a la izquierda, cuerpo abajo */
    var lockSvgOpen =
      '<svg viewBox="0 0 16 16" width="11" height="11" focusable="false">' +
        '<path fill="currentColor" d="M11.2 7.2V4.8a3.2 3.2 0 0 0-6.4-.15h1.35a1.85 1.85 0 0 1 3.7.15v2.4H11.2z"/>' +
        '<path fill="currentColor" d="M2.9 7.2h10.2c.72 0 1.3.58 1.3 1.3v5.2c0 .72-.58 1.3-1.3 1.3H2.9c-.72 0-1.3-.58-1.3-1.3V8.5c0-.72.58-1.3 1.3-1.3z"/>' +
      '</svg>';

    var lockHtml = '';
    if (isProduct && stockLockOpen === true) {
      lockHtml = '<span class="relmap-lock is-open" aria-hidden="true" title="Con stock">' + lockSvgOpen + '</span>';
    } else if (isProduct && stockLockOpen === false) {
      lockHtml = '<span class="relmap-lock is-out" aria-hidden="true" title="Sin stock">' + lockSvgClosed + '</span>';
    } else if (node.locked) {
      lockHtml = '<span class="relmap-lock" aria-hidden="true" title="Documento cerrado">' + lockSvgClosed + '</span>';
    }

    return '' +
      '<button type="button" class="' + classes + '" data-node-key="' + esc(node.key) + '"' +
        (node.view_url ? ' data-view-url="' + esc(node.view_url) + '"' : '') +
        (node.is_current ? ' data-current="1"' : '') +
        ' title="' + esc(title + ' ' + num) + '">' +
        '<div class="relmap-card-head">' +
          '<span class="relmap-card-title">' + esc(title) + '</span>' +
          lockHtml +
        '</div>' +
        '<div class="relmap-card-body">' +
          '<div class="relmap-card-num">' + esc(num) + '</div>' +
          metaHtml +
          (money ? '<div class="relmap-card-total">' + esc(money) + '</div>' : '') +
          (pay ? '<div class="relmap-card-pay">' + esc(pay) + '</div>' : '') +
          (status && !isPartner ? '<div class="relmap-card-status' + statusClass + '">' + esc(status) + '</div>' : '') +
        '</div>' +
      '</button>';
  }

  function edgePath(fromPos, toPos) {
    var x1 = fromPos.x + fromPos.w;
    var y1 = fromPos.y + fromPos.h / 2;
    var x2 = toPos.x;
    var y2 = toPos.y + toPos.h / 2;
    var dx = Math.max(40, (x2 - x1) * 0.45);
    return 'M ' + x1 + ' ' + y1 + ' C ' + (x1 + dx) + ' ' + y1 + ', ' + (x2 - dx) + ' ' + y2 + ', ' + x2 + ' ' + y2;
  }

  function render(container, mapData) {
    if (!container) return;
    var nodes = (mapData && mapData.nodes) || [];
    var edges = (mapData && mapData.edges) || [];
    if (!nodes.length) {
      container.innerHTML = '<div style="padding:16px;color:#64748b;font-size:13px;">Sin relaciones documentales para este producto.</div>';
      return;
    }
    var layout = layoutRelationshipMap(nodes, edges);
    var markerId = 'relmapArrow_' + String(Date.now()) + '_' + Math.floor(Math.random() * 9999);
    var cards = nodes.map(function (node) {
      var pos = layout.positions[node.key];
      if (!pos) return '';
      return '<div class="relmap-node-wrap" style="left:' + pos.x + 'px;top:' + pos.y + 'px;width:' + pos.w + 'px;height:' + pos.h + 'px;">' +
        buildMapCardHtml(node) +
        '</div>';
    }).join('');

    var paths = edges.map(function (e) {
      var a = layout.positions[e.from];
      var b = layout.positions[e.to];
      if (!a || !b) return '';
      return '<path class="relmap-edge" marker-end="url(#' + markerId + ')" d="' + edgePath(a, b) + '" />';
    }).join('');

    container.innerHTML =
      '<div class="relmap-canvas" style="width:' + layout.width + 'px;height:' + layout.height + 'px;">' +
        '<svg class="relmap-svg" width="' + layout.width + '" height="' + layout.height + '" aria-hidden="true">' +
          '<defs>' +
            '<marker id="' + markerId + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
              '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4a6078"></path>' +
            '</marker>' +
          '</defs>' +
          paths +
        '</svg>' +
        '<div class="relmap-nodes">' + cards + '</div>' +
      '</div>';
  }

  function withEmbed(url) {
    var u = String(url || '');
    if (!u) return u;
    if (/[?&]embed=/.test(u)) return u;
    return u + (u.indexOf('?') === -1 ? '?' : '&') + 'embed=1';
  }

  function withoutEmbed(url) {
    var u = String(url || '');
    return u
      .replace(/([?&])embed=\d+/g, '$1')
      .replace(/[?&]$/, '')
      .replace(/\?&/, '?')
      .replace(/&&+/g, '&');
  }

  function ensurePreviewModal() {
    var el = document.getElementById('relmapPreviewModal');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'relmapPreviewModal';
    el.className = 'relmap-psmodal relmap-preview-modal';
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML =
      '<div class="relmap-psbox relmap-preview-box">' +
        '<div class="relmap-pshead">' +
          '<div class="relmap-pshead-text">' +
            '<h3 id="relmapPreviewTitle">Documento</h3>' +
            '<p class="relmap-pshead-sub">Vista previa · el mapa permanece abierto</p>' +
          '</div>' +
          '<button type="button" class="relmap-psclosex" id="relmapPreviewClose" aria-label="Cerrar">&times;</button>' +
        '</div>' +
        '<div class="relmap-preview-frame-wrap">' +
          '<iframe id="relmapPreviewFrame" class="relmap-preview-frame" title="Vista previa del documento"></iframe>' +
        '</div>' +
        '<div class="relmap-psfoot">' +
          '<span class="relmap-hint">Podés cerrar y seguir en el mapa, o abrir la página completa.</span>' +
          '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<button type="button" class="relmap-btn" id="relmapPreviewClose2">Cerrar</button>' +
            '<a class="relmap-btn is-primary" id="relmapPreviewOpenFull" href="#" target="_top" rel="noopener">Abrir en página completa</a>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(el);

    function closePreview() {
      el.classList.remove('open');
      el.setAttribute('aria-hidden', 'true');
      var frame = document.getElementById('relmapPreviewFrame');
      if (frame) frame.src = 'about:blank';
    }

    var btn1 = document.getElementById('relmapPreviewClose');
    var btn2 = document.getElementById('relmapPreviewClose2');
    if (btn1) btn1.addEventListener('click', closePreview);
    if (btn2) btn2.addEventListener('click', closePreview);
    el.addEventListener('click', function (ev) {
      if (ev.target === el) closePreview();
    });
    el._closePreview = closePreview;
    return el;
  }

  function openPreview(url, title) {
    if (!url) return;
    var modal = ensurePreviewModal();
    var frame = document.getElementById('relmapPreviewFrame');
    var titleEl = document.getElementById('relmapPreviewTitle');
    var fullLink = document.getElementById('relmapPreviewOpenFull');
    var previewUrl = withEmbed(url);
    var fullUrl = withoutEmbed(url) || url;
    if (titleEl) titleEl.textContent = title || 'Documento';
    if (fullLink) {
      fullLink.href = fullUrl;
      fullLink.onclick = function (ev) {
        // Deja que navegue el top; no bloquea.
      };
    }
    if (frame) {
      frame.src = previewUrl;
    }
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
  }

  function bindClicks(container, opts) {
    if (!container || container._relmapBound) return;
    container._relmapBound = true;
    container.addEventListener('click', function (event) {
      var card = event.target.closest('.relmap-card');
      if (!card || !container.contains(card)) return;
      if (card.getAttribute('data-current') === '1') return;
      var url = card.getAttribute('data-view-url');
      if (!url) return;
      var title = card.getAttribute('title') || 'Documento';
      if (opts && typeof opts.onNavigate === 'function') {
        opts.onNavigate(url, { title: title, card: card });
        return;
      }
      // Por defecto: preview en modal (no salir de la pantalla)
      openPreview(url, title);
    });
  }

  global.AndesRelMap = {
    render: render,
    bindClicks: bindClicks,
    openPreview: openPreview,
    withEmbed: withEmbed,
    withoutEmbed: withoutEmbed,
    labelDocType: labelDocType
  };
})(window);
