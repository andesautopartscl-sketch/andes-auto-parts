"""FASE 10.3 — Secret Vault del asistente.

Paquete deliberadamente vacio de todo menos criptografia. En 10.3.1 solo existe
`vault_crypto`: bytes a bytes, sin base de datos, sin rutas, sin interfaz.

El orden importa. Mientras no haya almacen no hay forma de guardar un secreto,
y por tanto no hay nada que filtrar: el modulo que mas cuidado necesita se
escribe cuando todavia no puede hacer daño.
"""


# FASE 10.3.4-A — fachada. Lo UNICO que sale de este paquete.
#
# El Vault tiene ahora superficie HTTP, y esa superficie hay que registrarla
# desde `create_app()`. Se exporta el blueprint y nada mas: quien lo importe
# obtiene rutas, no `VaultStore`, ni `VaultKeyring`, ni el Broker. La frontera
# de 10.3.3 —nadie fuera del paquete toca las tripas— sigue en pie, y hay un
# test que la comprueba con el AST en vez de fiarse de esta nota.
from app.assistant.vault.vault_api import vault_bp  # noqa: E402

__all__ = ["vault_bp"]
