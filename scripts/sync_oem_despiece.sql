-- Generado por scripts/sync_oem_despiece.py --generate
-- Aplicar: python scripts/sync_oem_despiece.py --apply
BEGIN TRANSACTION;

-- oem_norm=10046260
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('10046260', NULL, 'Motor — conjunto pistón / anillos (demo)', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png', '[{"callout": "24", "part_no": "10046260", "usage": "RING KIT-PSTN", "qty": "4", "x_pct": 48, "y_pct": 36, "r_pct": 4.2, "price": "72.43", "ref_price": "94.16"}]', 'Registro demo: reemplazá por tu imagen en epc_despiece/ y actualizá imagen_static.', '2026-05-26 20:16:11.597880');

-- oem_norm=C00015176
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('C00015176', NULL, 'EJE PALIER IZQUIERDO PARA CAJA WIA 6 VELOCIDADES', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826578/andes_erp/epc_despiece/MAX180_1776110730.png', '[{"callout": "1", "part_no": "1", "usage": "", "qty": "1"}]', 'EURO 5 Y EURO 6', '2026-05-26 20:16:19.194471');

-- oem_norm=C00038336
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('C00038336', NULL, 'EJE PALIER DERECHO PARA CAJA WIA 6 VELOCIDADES', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826578/andes_erp/epc_despiece/MAX181_1776110765.png', '[{"callout": "1", "part_no": "2", "usage": "", "qty": "1"}]', 'EURO 5 Y 6', '2026-05-26 20:16:19.940119');

-- oem_norm=SX5-2906013
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('SX5-2906013', NULL, 'GOMA DE BARRA ESTABILIZADORA DELANTERA', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826579/andes_erp/epc_despiece/SX6043_1776106591.png', '[{"callout": "1", "part_no": "6", "usage": "", "qty": "1"}]', 'VIENEN PARTE 6 Y 7', '2026-05-26 20:16:20.837495');

-- oem_norm=_INT_VG52405
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('_INT_VG52405', 'VG52405', NULL, 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826580/andes_erp/epc_despiece/VG52405_1776102612.jpg', '[{"callout": "1", "part_no": "7", "usage": "", "qty": "1"}]', 'SENSOR ABS TRASERO DERECHO', '2026-05-26 20:16:21.596379');

-- oem_norm=_INT_VG52406
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('_INT_VG52406', 'VG52406', 'sensor abs trasero izquierdo', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826581/andes_erp/epc_despiece/VG52406_1776102682.jpg', '[{"callout": "1", "part_no": "7", "usage": "", "qty": "1"}]', NULL, '2026-05-26 20:16:22.595968');

-- oem_norm=_INT_VGP3092
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('_INT_VGP3092', 'VGP3092', 'Sensor abs delantero derecho', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826582/andes_erp/epc_despiece/VGP3092_1776102717.jpg', '[{"callout": "1", "part_no": "2", "usage": "", "qty": "1"}]', NULL, '2026-05-26 20:16:23.510813');

-- oem_norm=_INT_VGP3093
INSERT OR REPLACE INTO oem_despiece (oem_norm, producto_codigo, titulo, imagen_static, partes_json, notas, updated_at) VALUES ('_INT_VGP3093', 'VGP3093', 'Sensor abs delantero izquierdo', 'https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826583/andes_erp/epc_despiece/VGP3093_1776102747.jpg', '[{"callout": "1", "part_no": "1", "usage": "", "qty": "1"}]', NULL, '2026-05-26 20:16:24.402964');

COMMIT;
