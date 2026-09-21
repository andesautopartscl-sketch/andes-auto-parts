"""Paquete de tests del proyecto.

Existe por una razón concreta, no por costumbre: ``andes_agent/`` trae su propio
paquete ``tests`` CON ``__init__.py``. En el sistema de imports de Python un
paquete regular gana siempre sobre uno namespace, da igual el orden de
``sys.path`` — así que en cuanto algo ponía ``andes_agent/`` en la ruta, ``tests``
pasaba a resolverse contra el del Gateway y los 41 módulos de esta suite dejaban
de importar. Medido: la suite seguía pasando por separado mientras el closure
reportaba 41 errores de carga.

Con este archivo, ``tests`` es un paquete regular y decide el orden de la ruta,
que es lo que uno espera.
"""
