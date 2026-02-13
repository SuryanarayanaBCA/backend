import firebase_admin
from firebase_admin import credentials, auth
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

cred = credentials.Certificate(os.path.join(BASE_DIR, "firebase_key.json"))
firebase_admin.initialize_app(cred)

ADMIN_UID = "5Ose9lgY5dSX1xNDNj233iu42NQ2"

auth.set_custom_user_claims(ADMIN_UID, {
    "admin": True
})

print("✅ Admin access granted successfully")
