import os
import sys

from alloc_platform.db.models import User
from alloc_platform.db.session import SessionLocal, init_db

print("=== CHECKING SECURITY & HOST HYGIENE ===")

# 1. Check bootstrap_admin.txt across candidate paths
candidate_paths = [
    "/opt/alloc/current/bootstrap_admin.txt",
    "/opt/alloc/bootstrap_admin.txt",
    "/tmp/bootstrap_admin.txt",
    "/var/lib/alloc/bootstrap_admin.txt",
    "/home/ec2-user/bootstrap_admin.txt",
]
found = []
for p in candidate_paths:
    if os.path.exists(p):
        found.append(p)

if found:
    print(f"WARNING: bootstrap_admin.txt FOUND at: {found}")
else:
    print("CONFIRMED: bootstrap_admin.txt does NOT exist on host (checked all standard locations).")

# 2. Check admin user in DB
init_db()
db = SessionLocal()
users = db.query(User).all()
print(f"Total Users in DB: {len(users)}")
for u in users:
    print(f"  User: username={u.username}, role={u.role}, hash_len={len(u.password_hash) if u.password_hash else 0}, created_at={u.created_at}")
db.close()
