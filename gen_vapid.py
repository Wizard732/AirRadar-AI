"""gen_vapid.py — створює VAPID-ключі для Web Push. Приватний ключ (vapid_private.pem) НЕ коммітити; публічний кладемо в miniapp/vapid.json."""
import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
open("vapid_private.pem", "wb").write(key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
open("miniapp/vapid.json", "w").write(json.dumps({"public_key": base64.urlsafe_b64encode(pub).rstrip(b"=").decode()}))
print("vapid_private.pem + miniapp/vapid.json створено")
