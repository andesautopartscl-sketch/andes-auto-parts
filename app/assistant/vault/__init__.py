"""FASE 10.3 — Secret Vault del asistente.

Paquete deliberadamente vacio de todo menos criptografia. En 10.3.1 solo existe
`vault_crypto`: bytes a bytes, sin base de datos, sin rutas, sin interfaz.

El orden importa. Mientras no haya almacen no hay forma de guardar un secreto,
y por tanto no hay nada que filtrar: el modulo que mas cuidado necesita se
escribe cuando todavia no puede hacer daño.
"""
