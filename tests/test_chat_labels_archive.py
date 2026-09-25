from __future__ import annotations

import unittest

from app.utils.csrf import CSRF_SESSION_KEY


class ChatLabelsArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app
        from app.chat.models import DEFAULT_CHAT_LABELS
        from app.seguridad.models import Usuario

        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        cls.client = cls.app.test_client()

        users = (
            Usuario.query.filter_by(activo=True)
            .order_by(Usuario.id.asc())
            .all()
        )
        cls.owner = users[0] if users else None
        cls.other = next((u for u in users[1:] if u.id != getattr(cls.owner, "id", None)), None)
        cls.system_names = {nombre for _, nombre, _ in DEFAULT_CHAT_LABELS}

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["user"] = user.usuario
            sess["user_id"] = user.id
            sess["rol"] = user.rol.nombre if user.rol else None
            sess[CSRF_SESSION_KEY] = "csrf-chat-labels"
        return {"X-CSRF-Token": "csrf-chat-labels"}

    def test_catalogo_empresa_incluye_vendedor_y_transportista(self):
        self.assertIsNotNone(self.owner, "hace falta al menos un usuario activo")
        headers = self._login(self.owner)
        resp = self.client.get("/chat/api/labels", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json() or {}
        self.assertTrue(data.get("ok"))
        names = {item.get("nombre") for item in (data.get("labels") or [])}
        self.assertIn("Vendedor", names)
        self.assertIn("Transportista", names)
        self.assertTrue(self.system_names <= names)

    def test_archivar_y_etiquetar_conversacion(self):
        if self.owner is None or self.other is None:
            self.skipTest("hacen falta dos usuarios activos")
        headers = self._login(self.owner)

        labels_resp = self.client.get("/chat/api/labels", headers=headers)
        labels = (labels_resp.get_json() or {}).get("labels") or []
        vendedor = next((lb for lb in labels if lb.get("nombre") == "Vendedor"), None)
        transporte = next((lb for lb in labels if lb.get("nombre") == "Transportista"), None)
        self.assertIsNotNone(vendedor)
        self.assertIsNotNone(transporte)

        other_id = int(self.other.id)
        arch = self.client.post(
            f"/chat/api/conversations/{other_id}/archive",
            json={"archived": True},
            headers=headers,
        )
        self.assertEqual(arch.status_code, 200, arch.get_data(as_text=True))
        self.assertTrue((arch.get_json() or {}).get("archived"))

        tagged = self.client.put(
            f"/chat/api/conversations/{other_id}/labels",
            json={"label_ids": [vendedor["id"], transporte["id"]]},
            headers=headers,
        )
        self.assertEqual(tagged.status_code, 200, tagged.get_data(as_text=True))
        tagged_names = {item.get("nombre") for item in ((tagged.get_json() or {}).get("labels") or [])}
        self.assertEqual(tagged_names, {"Vendedor", "Transportista"})

        users_resp = self.client.get("/chat/api/users", headers=headers)
        self.assertEqual(users_resp.status_code, 200)
        row = next(
            (u for u in ((users_resp.get_json() or {}).get("users") or []) if int(u["id"]) == other_id),
            None,
        )
        self.assertIsNotNone(row)
        self.assertTrue(row.get("archived"))
        self.assertEqual({t.get("nombre") for t in (row.get("labels") or [])}, {"Vendedor", "Transportista"})

        created = self.client.post(
            "/chat/api/labels",
            json={"nombre": "Sucursal Test Chat", "color": "#0ea5e9"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        custom = (created.get_json() or {}).get("label") or {}
        self.assertEqual(custom.get("nombre"), "Sucursal Test Chat")
        self.assertFalse(custom.get("sistema"))

        # Limpieza: no dejar la conversación de prueba archivada ni la etiqueta extra.
        self.client.post(
            f"/chat/api/conversations/{other_id}/archive",
            json={"archived": False},
            headers=headers,
        )
        self.client.put(
            f"/chat/api/conversations/{other_id}/labels",
            json={"label_ids": []},
            headers=headers,
        )
        from app.chat.models import ChatLabel, ChatConversationLabel
        from app.extensions import db

        extra = ChatLabel.query.filter_by(id=custom.get("id")).first()
        if extra is not None and not extra.sistema:
            ChatConversationLabel.query.filter_by(label_id=extra.id).delete(synchronize_session=False)
            db.session.delete(extra)
            db.session.commit()

    def test_clave_archivados_bloquea_hasta_desbloquear(self):
        from werkzeug.security import generate_password_hash

        from app.chat.models import normalize_chat_archive_pin
        from app.extensions import db
        from app.seguridad.models import Usuario

        self.assertEqual(normalize_chat_archive_pin("2580"), "2580")
        with self.assertRaises(ValueError):
            normalize_chat_archive_pin("12")

        if self.owner is None:
            self.skipTest("hace falta un usuario activo")
        user = db.session.get(Usuario, self.owner.id)
        prev = user.chat_archive_pin_hash
        try:
            user.chat_archive_pin_hash = generate_password_hash("2580")
            db.session.commit()
            headers = self._login(user)
            listed = self.client.get("/chat/api/users", headers=headers)
            self.assertEqual(listed.status_code, 200)
            body = listed.get_json() or {}
            self.assertTrue(body.get("archive_locked"))
            self.assertFalse(body.get("archive_unlocked"))
            bad = self.client.post("/chat/api/archive-unlock", json={"pin": "0000"}, headers=headers)
            self.assertEqual(bad.status_code, 403)
            good = self.client.post("/chat/api/archive-unlock", json={"pin": "2580"}, headers=headers)
            self.assertEqual(good.status_code, 200, good.get_data(as_text=True))
            self.assertTrue((good.get_json() or {}).get("archive_unlocked"))
            listed2 = self.client.get("/chat/api/users", headers=headers).get_json() or {}
            self.assertTrue(listed2.get("archive_unlocked"))
        finally:
            user = db.session.get(Usuario, self.owner.id)
            user.chat_archive_pin_hash = prev
            db.session.commit()


if __name__ == "__main__":
    unittest.main()
