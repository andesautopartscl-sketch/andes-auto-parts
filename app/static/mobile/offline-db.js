(function (global) {
  "use strict";

  var DB_NAME = "andes-mobile-offline";
  var DB_VERSION = 1;
  var CATALOG_SCHEMA = 2;
  var CATALOG_STORE = "catalogo";
  var VENTAS_STORE = "ventas_recientes";
  var INGRESOS_STORE = "ingresos_recientes";
  var META_STORE = "meta";
  var RECENT_PRODUCTS_KEY = "recent_products";
  var META_CATALOG_OFFSET = "catalog_sync_offset";
  var META_CATALOG_TOTAL = "catalog_sync_total";
  var META_CATALOG_LOCK = "catalog_sync_lock";
  var CATALOG_TTL_MS = 24 * 60 * 60 * 1000;
  var CATALOG_PAGE_SIZE = 800;
  var CATALOG_BATCH_SIZE = 200;
  var LOCK_HEARTBEAT_MS = 3000;
  var LOCK_STALE_MS = 12000;
  var RECENT_LIMIT = 50;
  var RECENT_PRODUCTS_LIMIT = 12;

  var syncControl = {
    paused: false,
    sessionId: null,
    heartbeatTimer: null,
  };

  function openDb() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onerror = function () {
        reject(req.error || new Error("No se pudo abrir IndexedDB"));
      };
      req.onupgradeneeded = function (ev) {
        var db = ev.target.result;
        if (!db.objectStoreNames.contains(CATALOG_STORE)) {
          var cat = db.createObjectStore(CATALOG_STORE, { keyPath: "codigo" });
          cat.createIndex("descripcion", "descripcion", { unique: false });
        }
        if (!db.objectStoreNames.contains(VENTAS_STORE)) {
          db.createObjectStore(VENTAS_STORE, { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains(INGRESOS_STORE)) {
          db.createObjectStore(INGRESOS_STORE, { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains(META_STORE)) {
          db.createObjectStore(META_STORE, { keyPath: "key" });
        }
      };
      req.onsuccess = function () {
        resolve(req.result);
      };
    });
  }

  function txStore(db, store, mode) {
    return db.transaction(store, mode).objectStore(store);
  }

  function getMeta(db, key) {
    return new Promise(function (resolve) {
      var store = txStore(db, META_STORE, "readonly");
      var req = store.get(key);
      req.onsuccess = function () {
        resolve(req.result ? req.result.value : null);
      };
      req.onerror = function () {
        resolve(null);
      };
    });
  }

  function setMeta(db, key, value) {
    return new Promise(function (resolve, reject) {
      var store = txStore(db, META_STORE, "readwrite");
      var req = store.put({ key: key, value: value });
      req.onsuccess = function () {
        resolve();
      };
      req.onerror = function () {
        reject(req.error || new Error("No se pudo guardar meta"));
      };
    });
  }

  function clearCatalogStore(db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(CATALOG_STORE, "readwrite");
      var store = tx.objectStore(CATALOG_STORE);
      store.clear();
      tx.oncomplete = function () {
        resolve();
      };
      tx.onerror = function () {
        reject(tx.error || new Error("No se pudo limpiar el catálogo local"));
      };
    });
  }

  function putCatalogBatch(db, items, clearFirst) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(CATALOG_STORE, "readwrite");
      var store = tx.objectStore(CATALOG_STORE);
      if (clearFirst) {
        store.clear();
      }
      items.forEach(function (item) {
        store.put(item);
      });
      tx.oncomplete = function () {
        resolve(items.length);
      };
      tx.onerror = function () {
        reject(tx.error || new Error("No se pudo guardar lote del catálogo"));
      };
    });
  }

  function countCatalogItems(db) {
    return new Promise(function (resolve) {
      var store = txStore(db, CATALOG_STORE, "readonly");
      var req = store.count();
      req.onsuccess = function () {
        resolve(Number(req.result || 0));
      };
      req.onerror = function () {
        resolve(0);
      };
    });
  }

  function catalogUrlWithParams(apiUrl, offset, limit) {
    var url = new URL(apiUrl, window.location.origin);
    url.searchParams.set("offset", String(offset));
    url.searchParams.set("limit", String(limit));
    return url.pathname + url.search;
  }

  function fetchCatalogPage(apiUrl, offset, limit) {
    return fetch(catalogUrlWithParams(apiUrl, offset, limit), {
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
      credentials: "same-origin",
      cache: "no-store",
    }).then(function (res) {
      var ct = (res.headers.get("content-type") || "").toLowerCase();
      if (!res.ok || ct.indexOf("application/json") === -1) {
        var err = new Error("Catálogo HTTP " + res.status);
        err.status = res.status;
        throw err;
      }
      return res.json();
    }).then(function (data) {
      if (!data || data.success === false) {
        throw new Error((data && data.message) || "Respuesta inválida del catálogo");
      }
      return data;
    });
  }

  function normalizeText(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  function splitTerms(query) {
    var norm = normalizeText(query);
    if (!norm) return [];
    return norm.split(/\s+/).filter(Boolean);
  }

  function fieldBlob(row) {
    if (row.search_text) return row.search_text;
    return normalizeText(
      [
        row.codigo,
        row.descripcion,
        row.modelo,
        row.motor,
        row.marca,
        row.codigo_oem,
        row.codigo_alternativo,
        row.homologados,
        row.medidas,
        row.anio,
        row.version,
      ]
        .filter(Boolean)
        .join(" ")
    );
  }

  function termInField(term, key, row) {
    var val = normalizeText(row[key] || "");
    if (!term || !val) return false;
    if (val.indexOf(term) !== -1) return true;
    if (key === "homologados") {
      var tokens = val.split(/[^a-z0-9]+/).filter(Boolean);
      return tokens.indexOf(term) !== -1;
    }
    return false;
  }

  function scoreRow(row, terms, rawQuery) {
    var blob = fieldBlob(row);
    for (var i = 0; i < terms.length; i++) {
      if (blob.indexOf(terms[i]) === -1) return null;
    }
    var codigo = normalizeText(row.codigo);
    var rawNorm = normalizeText(rawQuery).replace(/\s+/g, "");
    if (codigo && (codigo === rawNorm || codigo === normalizeText(rawQuery))) {
      return { rank: 0, match_en: "Código" };
    }
    for (var t = 0; t < terms.length; t++) {
      if (codigo === terms[t]) return { rank: 0, match_en: "Código" };
    }
    for (var t2 = 0; t2 < terms.length; t2++) {
      if (termInField(terms[t2], "codigo_oem", row)) return { rank: 1, match_en: "OEM" };
      if (termInField(terms[t2], "codigo_alternativo", row)) return { rank: 1, match_en: "Alternativo" };
      if (termInField(terms[t2], "homologados", row)) return { rank: 1, match_en: "Homologado" };
    }
    var desc = normalizeText(row.descripcion);
    if (desc && terms[0] && desc.indexOf(terms[0]) === 0) {
      return { rank: 2, match_en: "Descripción" };
    }
    var fields = ["medidas", "motor", "modelo", "marca", "anio", "version", "descripcion"];
    var labels = {
      medidas: "Medidas",
      motor: "Motor",
      modelo: "Modelo",
      marca: "Marca",
      anio: "Año",
      version: "Versión",
      descripcion: "Descripción",
    };
    for (var t3 = 0; t3 < terms.length; t3++) {
      for (var f = 0; f < fields.length; f++) {
        if (termInField(terms[t3], fields[f], row)) {
          return { rank: 3, match_en: labels[fields[f]] };
        }
      }
    }
    return { rank: 3, match_en: "Descripción" };
  }

  function searchCatalogLocal(db, query, limit) {
    var terms = splitTerms(query);
    var raw = String(query || "").trim();
    if (raw.length < 2 || !terms.length) return Promise.resolve([]);
    limit = limit || 50;
    return new Promise(function (resolve) {
      var scored = [];
      var store = txStore(db, CATALOG_STORE, "readonly");
      var req = store.openCursor();
      req.onsuccess = function (ev) {
        var cursor = ev.target.result;
        if (!cursor) {
          scored.sort(function (a, b) {
            if (a.rank !== b.rank) return a.rank - b.rank;
            return String(a.row.codigo || "").localeCompare(String(b.row.codigo || ""));
          });
          resolve(
            scored.slice(0, limit).map(function (item) {
              var row = item.row;
              return {
                codigo: row.codigo,
                descripcion: row.descripcion,
                stock: row.stock,
                precio_fmt: row.precio_fmt || "—",
                imagen: row.imagen || "",
                meta_linea: row.meta_linea || "",
                match_en: item.match_en,
              };
            })
          );
          return;
        }
        var row = cursor.value || {};
        var match = scoreRow(row, terms, raw);
        if (match) {
          scored.push({ rank: match.rank, match_en: match.match_en, row: row });
        }
        cursor.continue();
      };
      req.onerror = function () {
        resolve([]);
      };
    });
  }

  function getCatalogProduct(db, codigo) {
    var key = String(codigo || "").trim().toUpperCase();
    if (!key) return Promise.resolve(null);
    return new Promise(function (resolve) {
      var store = txStore(db, CATALOG_STORE, "readonly");
      var req = store.get(key);
      req.onsuccess = function () {
        resolve(req.result || null);
      };
      req.onerror = function () {
        resolve(null);
      };
    });
  }

  function pushRecent(db, storeName, record) {
    if (!record || record.id == null) return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(storeName, "readwrite");
      var store = tx.objectStore(storeName);
      store.put(record);
      var seen = 0;
      var cursorReq = store.openCursor(null, "prev");
      cursorReq.onsuccess = function (ev) {
        var cursor = ev.target.result;
        if (cursor) {
          seen += 1;
          if (seen > RECENT_LIMIT) {
            store.delete(cursor.value.id);
          }
          cursor.continue();
        }
      };
      tx.oncomplete = function () {
        resolve();
      };
      tx.onerror = function () {
        reject(tx.error);
      };
    });
  }

  function generateSessionId() {
    return (
      Date.now().toString(36) +
      "-" +
      Math.random().toString(36).slice(2, 10)
    );
  }

  function shouldSyncCatalog(lastSync, schemaVersion) {
    if (Number(schemaVersion) !== CATALOG_SCHEMA) return true;
    if (!lastSync) return true;
    return Date.now() - Number(lastSync) > CATALOG_TTL_MS;
  }

  function isCatalogReady(db) {
    return Promise.all([getMeta(db, "catalog_schema"), countCatalogItems(db)]).then(
      function (parts) {
        var schemaVersion = parts[0];
        var count = Number(parts[1] || 0);
        if (Number(schemaVersion) !== CATALOG_SCHEMA) return false;
        return count >= 1;
      }
    );
  }

  function isCatalogFresh(db) {
    return Promise.all([getMeta(db, "catalog_synced_at"), getMeta(db, "catalog_schema")]).then(
      function (parts) {
        return !shouldSyncCatalog(parts[0], parts[1]);
      }
    );
  }

  function getCatalogSyncProgress(db) {
    return Promise.all([
      getMeta(db, META_CATALOG_OFFSET),
      getMeta(db, META_CATALOG_TOTAL),
      getMeta(db, "catalog_synced_at"),
    ]).then(function (parts) {
      var offset = Number(parts[0] || 0);
      var total = Number(parts[1] || 0);
      var syncedAt = parts[2];
      var inProgress = total > 0 && offset > 0 && offset < total;
      if (!inProgress && !syncedAt && offset > 0 && total > 0 && offset >= total) {
        inProgress = false;
      }
      return {
        done: offset,
        total: total,
        inProgress: inProgress || (!syncedAt && offset > 0 && total > 0 && offset < total),
        complete: !!syncedAt && (!total || offset >= total),
      };
    });
  }

  function assessCatalogSyncNeed(db) {
    return Promise.all([
      getMeta(db, "catalog_synced_at"),
      getMeta(db, "catalog_schema"),
      getMeta(db, META_CATALOG_OFFSET),
      getMeta(db, META_CATALOG_TOTAL),
    ]).then(function (parts) {
      var lastSync = parts[0];
      var schemaVersion = parts[1];
      var offset = Number(parts[2] || 0);
      var total = Number(parts[3] || 0);

      if (Number(schemaVersion) !== CATALOG_SCHEMA) {
        return { needed: true, reason: "schema", startOffset: 0, total: null, clearFirst: true };
      }

      if (total > 0 && offset > 0 && offset < total) {
        return {
          needed: true,
          reason: "resume",
          startOffset: offset,
          total: total,
          clearFirst: false,
        };
      }

      if (!lastSync) {
        return {
          needed: true,
          reason: offset > 0 ? "resume" : "never",
          startOffset: offset > 0 ? offset : 0,
          total: total || null,
          clearFirst: offset <= 0,
        };
      }

      if (Date.now() - Number(lastSync) > CATALOG_TTL_MS) {
        return { needed: true, reason: "expired", startOffset: 0, total: null, clearFirst: true };
      }

      return { needed: false, reason: "fresh", startOffset: 0, total: total, clearFirst: false };
    });
  }

  function tryAcquireCatalogLock(db, sessionId, force) {
    return getMeta(db, META_CATALOG_LOCK).then(function (lock) {
      var now = Date.now();
      if (
        lock &&
        lock.owner &&
        lock.owner !== sessionId &&
        now - Number(lock.heartbeat || 0) < LOCK_STALE_MS &&
        !force
      ) {
        return { acquired: false, lock: lock };
      }
      return setMeta(db, META_CATALOG_LOCK, { owner: sessionId, heartbeat: now }).then(
        function () {
          return { acquired: true };
        }
      );
    });
  }

  function renewCatalogLock(db, sessionId) {
    return setMeta(db, META_CATALOG_LOCK, { owner: sessionId, heartbeat: Date.now() });
  }

  function releaseCatalogLock(db, sessionId) {
    return getMeta(db, META_CATALOG_LOCK).then(function (lock) {
      if (lock && lock.owner === sessionId) {
        return setMeta(db, META_CATALOG_LOCK, null);
      }
    });
  }

  function clearCatalogSyncProgress(db) {
    return Promise.all([
      setMeta(db, META_CATALOG_OFFSET, null),
      setMeta(db, META_CATALOG_TOTAL, null),
    ]);
  }

  function saveCatalogSyncProgress(db, offset, total) {
    return Promise.all([
      setMeta(db, META_CATALOG_OFFSET, offset),
      setMeta(db, META_CATALOG_TOTAL, total),
    ]);
  }

  function waitIfSyncPaused() {
    if (!syncControl.paused) return Promise.resolve();
    return new Promise(function (resolve) {
      function tick() {
        if (!syncControl.paused) {
          resolve();
          return;
        }
        setTimeout(tick, 350);
      }
      tick();
    });
  }

  function startLockHeartbeat(db, sessionId) {
    stopLockHeartbeat();
    syncControl.heartbeatTimer = setInterval(function () {
      renewCatalogLock(db, sessionId).catch(function () {});
    }, LOCK_HEARTBEAT_MS);
  }

  function stopLockHeartbeat() {
    if (syncControl.heartbeatTimer) {
      clearInterval(syncControl.heartbeatTimer);
      syncControl.heartbeatTimer = null;
    }
  }

  function runPaginatedCatalogSync(db, apiUrl, options) {
    var pageSize = options.pageSize || CATALOG_PAGE_SIZE;
    var onProgress = options.onProgress;
    var sessionId = options.sessionId || generateSessionId();
    var offset = Number(options.startOffset || 0);
    var total = options.total != null ? Number(options.total) : null;
    var written = offset;
    var clearPending = !!options.clearFirst;
    syncControl.sessionId = sessionId;
    syncControl.paused = false;

    function reportProgress() {
      if (onProgress) {
        onProgress({
          done: written,
          total: total || written,
          phase: "sync",
        });
      }
    }

    function finalize(count) {
      return clearCatalogSyncProgress(db)
        .then(function () {
          return setMeta(db, "catalog_synced_at", Date.now());
        })
        .then(function () {
          return setMeta(db, "catalog_schema", CATALOG_SCHEMA);
        })
        .then(function () {
          reportProgress();
          return { skipped: false, count: count, resumed: offset > 0 };
        });
    }

    function writeItemsInBatches(items, clearFirst) {
      var batchOffset = 0;
      var first = clearFirst;
      function nextBatch() {
        if (batchOffset >= items.length) return Promise.resolve();
        var slice = items.slice(batchOffset, batchOffset + CATALOG_BATCH_SIZE);
        return putCatalogBatch(db, slice, first).then(function () {
          batchOffset += slice.length;
          first = false;
          return nextBatch();
        });
      }
      return nextBatch();
    }

    function fetchNextPage() {
      return waitIfSyncPaused().then(function () {
        return fetchCatalogPage(apiUrl, offset, pageSize);
      }).then(function (data) {
        var items = data.items || [];
        if (total === null) {
          total = Number(data.total || data.count || 0);
        }
        if (!items.length) {
          if (written === 0) {
            return clearCatalogStore(db).then(function () {
              return finalize(0);
            });
          }
          return finalize(written);
        }
        var clearFirst = clearPending;
        clearPending = false;
        return writeItemsInBatches(items, clearFirst).then(function () {
          written += items.length;
          offset += items.length;
          return saveCatalogSyncProgress(db, offset, total).then(function () {
            reportProgress();
            if (items.length < pageSize || (total && offset >= total)) {
              return finalize(written);
            }
            return waitIfSyncPaused().then(fetchNextPage);
          });
        });
      });
    }

    startLockHeartbeat(db, sessionId);
    return setMeta(db, "catalog_schema", CATALOG_SCHEMA).then(function () {
      return fetchNextPage();
    }).finally(function () {
      stopLockHeartbeat();
      return releaseCatalogLock(db, sessionId);
    });
  }

  var AndesOfflineDb = {
    CATALOG_TTL_MS: CATALOG_TTL_MS,
    CATALOG_SCHEMA: CATALOG_SCHEMA,
    LOCK_STALE_MS: LOCK_STALE_MS,
    open: openDb,
    shouldSyncCatalog: shouldSyncCatalog,
    getMeta: getMeta,
    setMeta: setMeta,
    pauseCatalogSync: function () {
      syncControl.paused = true;
    },
    resumeCatalogSync: function () {
      syncControl.paused = false;
    },
    isCatalogSyncPaused: function () {
      return syncControl.paused;
    },
    releaseSyncSession: function (sessionId) {
      stopLockHeartbeat();
      syncControl.paused = false;
      if (!sessionId) return Promise.resolve();
      return openDb().then(function (db) {
        return releaseCatalogLock(db, sessionId);
      });
    },
    getCatalogSyncProgress: function () {
      return openDb().then(getCatalogSyncProgress);
    },
    assessCatalogSyncNeed: function () {
      return openDb().then(assessCatalogSyncNeed);
    },
    countCatalog: function () {
      return openDb().then(countCatalogItems);
    },
    isCatalogReady: function () {
      return openDb().then(isCatalogReady);
    },
    isCatalogFresh: function () {
      return openDb().then(isCatalogFresh);
    },
    syncCatalog: function (apiUrl, options) {
      options = options || {};
      if (!apiUrl) {
        return Promise.reject(new Error("URL de catálogo no configurada"));
      }
      var sessionId = options.sessionId || generateSessionId();
      return openDb().then(function (db) {
        function runSync(plan) {
          return runPaginatedCatalogSync(db, apiUrl, {
            pageSize: options.pageSize || CATALOG_PAGE_SIZE,
            onProgress: options.onProgress,
            sessionId: sessionId,
            startOffset: plan.startOffset,
            total: plan.total,
            clearFirst: plan.clearFirst,
          });
        }

        function prepareForce() {
          return clearCatalogStore(db)
            .then(function () {
              return setMeta(db, "catalog_synced_at", null);
            })
            .then(function () {
              return setMeta(db, "catalog_schema", null);
            })
            .then(function () {
              return clearCatalogSyncProgress(db);
            })
            .then(function () {
              return {
                needed: true,
                reason: "force",
                startOffset: 0,
                total: null,
                clearFirst: true,
              };
            });
        }

        var planPromise = options.force
          ? prepareForce()
          : assessCatalogSyncNeed(db).then(function (plan) {
              if (!plan.needed) {
                return { needed: false };
              }
              return plan;
            });

        return planPromise.then(function (plan) {
          if (!plan.needed) {
            return { skipped: true, count: 0 };
          }
          return tryAcquireCatalogLock(db, sessionId, !!options.force).then(function (lock) {
            if (!lock.acquired) {
              return { skipped: true, lockHeld: true, count: 0 };
            }
            if (plan.reason === "schema" || plan.reason === "expired" || plan.reason === "force") {
              return clearCatalogStore(db)
                .then(function () {
                  return setMeta(db, "catalog_synced_at", null);
                })
                .then(function () {
                  return clearCatalogSyncProgress(db);
                })
                .then(function () {
                  return setMeta(db, "catalog_schema", CATALOG_SCHEMA);
                })
                .then(function () {
                  return runSync({
                    startOffset: 0,
                    total: null,
                    clearFirst: true,
                  });
                });
            }
            return runSync(plan);
          });
        });
      });
    },
    searchLocal: function (query, limit) {
      return openDb().then(function (db) {
        return searchCatalogLocal(db, query, limit);
      });
    },
    getProduct: function (codigo) {
      return openDb().then(function (db) {
        return getCatalogProduct(db, codigo);
      });
    },
    recordVenta: function (venta) {
      return openDb().then(function (db) {
        return pushRecent(db, VENTAS_STORE, venta);
      });
    },
    recordIngreso: function (ingreso) {
      return openDb().then(function (db) {
        return pushRecent(db, INGRESOS_STORE, ingreso);
      });
    },
    getRecentVentas: function () {
      return openDb().then(function (db) {
        return new Promise(function (resolve) {
          var out = [];
          var store = txStore(db, VENTAS_STORE, "readonly");
          var req = store.openCursor(null, "prev");
          req.onsuccess = function (ev) {
            var cursor = ev.target.result;
            if (!cursor || out.length >= RECENT_LIMIT) {
              resolve(out);
              return;
            }
            out.push(cursor.value);
            cursor.continue();
          };
          req.onerror = function () {
            resolve([]);
          };
        });
      });
    },
    getRecentIngresos: function () {
      return openDb().then(function (db) {
        return new Promise(function (resolve) {
          var out = [];
          var store = txStore(db, INGRESOS_STORE, "readonly");
          var req = store.openCursor(null, "prev");
          req.onsuccess = function (ev) {
            var cursor = ev.target.result;
            if (!cursor || out.length >= RECENT_LIMIT) {
              resolve(out);
              return;
            }
            out.push(cursor.value);
            cursor.continue();
          };
          req.onerror = function () {
            resolve([]);
          };
        });
      });
    },
    recordProduct: function (codigo, descripcion) {
      var key = String(codigo || "").trim().toUpperCase();
      if (!key) return Promise.resolve();
      return openDb().then(function (db) {
        return getMeta(db, RECENT_PRODUCTS_KEY).then(function (list) {
          var items = Array.isArray(list) ? list.slice() : [];
          items = items.filter(function (p) {
            return (p.codigo || "").toUpperCase() !== key;
          });
          items.unshift({
            codigo: key,
            descripcion: String(descripcion || "").trim(),
            visited_at: Date.now(),
          });
          if (items.length > RECENT_PRODUCTS_LIMIT) {
            items = items.slice(0, RECENT_PRODUCTS_LIMIT);
          }
          return setMeta(db, RECENT_PRODUCTS_KEY, items);
        });
      });
    },
    getRecentProducts: function (limit) {
      limit = limit || 3;
      return openDb().then(function (db) {
        return getMeta(db, RECENT_PRODUCTS_KEY).then(function (list) {
          if (!Array.isArray(list)) return [];
          return list.slice(0, limit);
        });
      });
    },
    clearAll: function () {
      return openDb().then(function (db) {
        var names = Array.from(db.objectStoreNames);
        if (!names.length) return;
        return new Promise(function (resolve, reject) {
          var tx = db.transaction(names, "readwrite");
          names.forEach(function (name) {
            tx.objectStore(name).clear();
          });
          tx.oncomplete = function () {
            resolve();
          };
          tx.onerror = function () {
            reject(tx.error);
          };
        });
      });
    },
  };

  global.AndesOfflineDb = AndesOfflineDb;
})(typeof window !== "undefined" ? window : self);
