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
  var CATALOG_TTL_MS = 30 * 60 * 1000;
  var CATALOG_PAGE_SIZE = 1500;
  var CATALOG_BATCH_SIZE = 400;
  var RECENT_LIMIT = 50;
  var RECENT_PRODUCTS_LIMIT = 12;

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

  function shouldSyncCatalog(lastSync, schemaVersion) {
    if (Number(schemaVersion) !== CATALOG_SCHEMA) return true;
    if (!lastSync) return true;
    return Date.now() - Number(lastSync) > CATALOG_TTL_MS;
  }

  function runPaginatedCatalogSync(db, apiUrl, options) {
    var pageSize = options.pageSize || CATALOG_PAGE_SIZE;
    var onProgress = options.onProgress;
    var offset = 0;
    var total = null;
    var written = 0;
    var clearPending = true;

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
      return setMeta(db, "catalog_synced_at", Date.now()).then(function () {
        return setMeta(db, "catalog_schema", CATALOG_SCHEMA).then(function () {
          reportProgress();
          return { skipped: false, count: count };
        });
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
      return fetchCatalogPage(apiUrl, offset, pageSize).then(function (data) {
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
          reportProgress();
          if (items.length < pageSize || (total && offset >= total)) {
            return finalize(written);
          }
          return fetchNextPage();
        });
      });
    }

    return fetchNextPage();
  }

  var AndesOfflineDb = {
    CATALOG_TTL_MS: CATALOG_TTL_MS,
    CATALOG_SCHEMA: CATALOG_SCHEMA,
    open: openDb,
    shouldSyncCatalog: shouldSyncCatalog,
    getMeta: getMeta,
    setMeta: setMeta,
    countCatalog: function () {
      return openDb().then(countCatalogItems);
    },
    syncCatalog: function (apiUrl, options) {
      options = options || {};
      if (!apiUrl) {
        return Promise.reject(new Error("URL de catálogo no configurada"));
      }
      return openDb().then(function (db) {
        return Promise.all([
          getMeta(db, "catalog_synced_at"),
          getMeta(db, "catalog_schema"),
        ]).then(function (meta) {
          var lastSync = meta[0];
          var schemaVersion = meta[1];
          if (!options.force && !shouldSyncCatalog(lastSync, schemaVersion)) {
            return { skipped: true, count: 0 };
          }
          var needsSchemaReset = Number(schemaVersion) !== CATALOG_SCHEMA;
          var startSync = function () {
            return runPaginatedCatalogSync(db, apiUrl, {
              pageSize: options.pageSize || CATALOG_PAGE_SIZE,
              onProgress: options.onProgress,
            });
          };
          if (needsSchemaReset) {
            return clearCatalogStore(db)
              .then(function () {
                return setMeta(db, "catalog_synced_at", null);
              })
              .then(function () {
                return setMeta(db, "catalog_schema", null);
              })
              .then(startSync);
          }
          return startSync();
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
