-- Generado por scripts/sync_cloudinary_urls.py --generate
-- Aplicar en producción: python scripts/sync_cloudinary_urls.py --apply
-- oem_despiece: clave oem_norm (los id locales no coinciden con Render)
BEGIN TRANSACTION;

-- oem_despiece (8 registros)
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE oem_norm='10046260';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826578/andes_erp/epc_despiece/MAX180_1776110730.png' WHERE oem_norm='C00015176';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826578/andes_erp/epc_despiece/MAX181_1776110765.png' WHERE oem_norm='C00038336';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826579/andes_erp/epc_despiece/SX6043_1776106591.png' WHERE oem_norm='SX5-2906013';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826580/andes_erp/epc_despiece/VG52405_1776102612.jpg' WHERE oem_norm='_INT_VG52405';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826581/andes_erp/epc_despiece/VG52406_1776102682.jpg' WHERE oem_norm='_INT_VG52406';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826582/andes_erp/epc_despiece/VGP3092_1776102717.jpg' WHERE oem_norm='_INT_VGP3092';
UPDATE oem_despiece SET imagen_static='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826583/andes_erp/epc_despiece/VGP3093_1776102747.jpg' WHERE oem_norm='_INT_VGP3093';

-- productos.despiece (15 registros)
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826574/andes_erp/epc_despiece/3770100-E06.png' WHERE CODIGO='2417';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826577/andes_erp/epc_despiece/4121020-BQ01.png' WHERE CODIGO='CS4022RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826576/andes_erp/epc_despiece/4121010-BQ01.png' WHERE CODIGO='CS4023RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826574/andes_erp/epc_despiece/3770100-E06.png' WHERE CODIGO='JB106RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826574/andes_erp/epc_despiece/2803128XST01A.png' WHERE CODIGO='JO012';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE CODIGO='M3414RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE CODIGO='M3703RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE CODIGO='M3826RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE CODIGO='M4136ORG';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826570/andes_erp/epc_despiece/10046260.png' WHERE CODIGO='M4136RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826571/andes_erp/epc_despiece/10423089.png' WHERE CODIGO='M5011RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826572/andes_erp/epc_despiece/10423090.png' WHERE CODIGO='M5012RC';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826573/andes_erp/epc_despiece/10692830.png' WHERE CODIGO='MG6004';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826572/andes_erp/epc_despiece/10692829.png' WHERE CODIGO='MG6005';
UPDATE productos SET despiece='https://res.cloudinary.com/dhiybfj3u/image/upload/v1779826574/andes_erp/epc_despiece/3770100-E06.png' WHERE CODIGO='UR10402';

COMMIT;
